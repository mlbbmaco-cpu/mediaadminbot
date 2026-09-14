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

        # User sessions
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                started_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)

        # Admin-side bot messages
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_id INTEGER NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            )
        """)

        # Admin message -> user mapping
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_users (
                telegram_message_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        # Blocked users
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked_at INTEGER NOT NULL
            )
        """)

        # User-side bot messages
        #
        # These are the messages SENT BY THE BOT to the user.
        # They can be deleted when /block or /endchat is used.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
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
            (
                user_id,
                username or "",
                started,
                expires,
            ),
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

        conn.execute(
            """
            INSERT OR REPLACE INTO blocked_users
            (user_id, blocked_at)
            VALUES (?, ?)
            """,
            (
                user_id,
                now_ts(),
            ),
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
# ADMIN MESSAGE STORAGE
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
            (
                message_id,
                current,
            ),
        )

        if user_id is not None:
            conn.execute(
                """
                INSERT OR REPLACE INTO message_users
                (telegram_message_id, user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    message_id,
                    user_id,
                    current,
                ),
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
# USER-SIDE MESSAGE STORAGE
# ============================================================

def save_user_message(user_id, message_id):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_messages
            (user_id, telegram_message_id, created_at)
            VALUES (?, ?, ?)
            """,
            (
                user_id,
                message_id,
                now_ts(),
            ),
        )

        conn.commit()


def get_user_messages(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT id, telegram_message_id
            FROM user_messages
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()


def delete_user_message_record(record_id):
    with db() as conn:
        conn.execute(
            """
            DELETE FROM user_messages
            WHERE id = ?
            """,
            (record_id,),
        )

        conn.commit()


# ============================================================
# DELETE ONLY BOT MESSAGES FROM USER SIDE
# ============================================================

async def clear_user_side(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    """
    Deletes only messages that OUR BOT sent to the user.

    It does NOT touch:
    - Admin chat
    - Admin messages
    - User's own Telegram messages
    """

    rows = get_user_messages(user_id)

    for record_id, message_id in rows:

        try:
            await context.bot.delete_message(
                chat_id=user_id,
                message_id=message_id,
            )

            logger.info(
                "Deleted user-side bot message %s for user %s",
                message_id,
                user_id,
            )

        except Exception as exc:
            logger.info(
                "Could not delete user-side message %s for user %s: %s",
                message_id,
                user_id,
                exc,
            )

        delete_user_message_record(record_id)


# ============================================================
# SEND MESSAGE TO USER AND TRACK IT
# ============================================================

async def send_user_message(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
):
    """
    Sends a bot message to the user and stores its message ID
    so it can later be deleted by /block or /endchat.
    """

    try:
        sent = await context.bot.send_message(
            chat_id=user_id,
            text=text,
        )

        save_user_message(
            user_id,
            sent.message_id,
        )

        return sent

    except Exception as exc:
        logger.info(
            "Could not send message to user %s: %s",
            user_id,
            exc,
        )

        return None


# ============================================================
# OLD ADMIN MESSAGE CLEANUP
# ============================================================

async def cleanup_admin_messages(
    context: ContextTypes.DEFAULT_TYPE,
):
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

async def expire_sessions(
    context: ContextTypes.DEFAULT_TYPE,
):
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

    for (user_id,) in rows:

        # Blocked users stay completely silent
        if is_blocked(user_id):
            continue

        await send_user_message(
            context,
            user_id,
            (
                "⏰ Your chat session has ended.\n\n"
                "If you want to contact the admin again, "
                "send /start."
            ),
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
    # BLOCKED USERS ARE COMPLETELY SILENT
    # --------------------------------------------------------

    if is_blocked(user.id):
        return

    session = get_session(user.id)
    current = now_ts()

    # --------------------------------------------------------
    # ALREADY ACTIVE
    # --------------------------------------------------------

    if (
        session
        and session[4] == 1
        and session[3] > current
    ):

        sent = await message.reply_text(
            "⚠️ You are already in an active chat.\n\n"
            "Please send your message. "
            "The admin will contact you as soon as possible."
        )

        # Track bot message
        save_user_message(
            user.id,
            sent.message_id,
        )

        return

    # --------------------------------------------------------
    # NEW SESSION
    # --------------------------------------------------------

    create_session(
        user.id,
        user.username,
    )

    sent = await message.reply_text(
        "🔐 Anonymous chat started.\n\n"
        "Please send your message. "
        "The admin will contact you as soon as possible."
    )

    # Track bot message
    save_user_message(
        user.id,
        sent.message_id,
    )


# ============================================================
# USER INFO FOR ADMIN
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
# GET USER FROM ADMIN REPLY
# ============================================================

def get_reply_target_user_id(message):
    if not message.reply_to_message:
        return None

    replied = message.reply_to_message

    # First use database mapping
    user_id = get_user_from_admin_message(
        replied.message_id
    )

    if user_id:
        return user_id

    # Fallback: read User ID from info message
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

    # Only admin
    if not user or user.id != ADMIN_ID:
        return

    if not message:
        return

    target_user_id = None

    # --------------------------------------------------------
    # Reply to user's message
    # --------------------------------------------------------

    if message.reply_to_message:
        target_user_id = get_reply_target_user_id(
            message
        )

    # --------------------------------------------------------
    # /block USER_ID
    # --------------------------------------------------------

    if not target_user_id and context.args:

        try:
            target_user_id = int(
                context.args[0]
            )

        except ValueError:
            target_user_id = None

    if not target_user_id:

        await message.reply_text(
            "⚠️ Reply to a user's message with /block "
            "or use /block USER_ID."
        )

        return

    if target_user_id == ADMIN_ID:

        await message.reply_text(
            "❌ You cannot block yourself."
        )

        return

    # --------------------------------------------------------
    # BLOCK USER
    # --------------------------------------------------------

    block_user(target_user_id)

    # --------------------------------------------------------
    # IMPORTANT:
    # ONLY USER SIDE IS CLEARED.
    #
    # ADMIN SIDE IS NOT TOUCHED.
    # --------------------------------------------------------

    await clear_user_side(
        context,
        target_user_id,
    )

    # --------------------------------------------------------
    # ADMIN CONFIRMATION
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

    if not user or user.id != ADMIN_ID:
        return

    if not message:
        return

    target_user_id = None

    # --------------------------------------------------------
    # Reply method
    # --------------------------------------------------------

    if message.reply_to_message:

        target_user_id = get_reply_target_user_id(
            message
        )

    # --------------------------------------------------------
    # /unblock USER_ID
    # --------------------------------------------------------

    if not target_user_id and context.args:

        try:
            target_user_id = int(
                context.args[0]
            )

        except ValueError:
            target_user_id = None

    if not target_user_id:

        await message.reply_text(
            "⚠️ Reply to a user's message with /unblock "
            "or use /unblock USER_ID."
        )

        return

    # --------------------------------------------------------
    # UNBLOCK
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

    if not user or user.id != ADMIN_ID:
        return

    if not message:
        return

    target_user_id = None

    # --------------------------------------------------------
    # Reply method
    # --------------------------------------------------------

    if message.reply_to_message:

        target_user_id = get_reply_target_user_id(
            message
        )

    # --------------------------------------------------------
    # /endchat USER_ID
    # --------------------------------------------------------

    if not target_user_id and context.args:

        try:
            target_user_id = int(
                context.args[0]
            )

        except ValueError:
            target_user_id = None

    if not target_user_id:

        await message.reply_text(
            "⚠️ Reply to a user's message with /endchat "
            "or use /endchat USER_ID."
        )

        return

    # --------------------------------------------------------
    # END SESSION
    # --------------------------------------------------------

    end_session(target_user_id)

    # --------------------------------------------------------
    # USER-SIDE ONLY
    #
    # Send ending notification first.
    # Then clear all bot-generated user-side messages,
    # including this one.
    # --------------------------------------------------------

    if not is_blocked(target_user_id):

        await send_user_message(
            context,
            target_user_id,
            (
                "⏹️ Your chat session has ended.\n\n"
                "If you want to contact the admin again, "
                "send /start."
            ),
        )

    # --------------------------------------------------------
    # CLEAR ONLY USER SIDE
    #
    # NOTHING IS DELETED FROM ADMIN CHAT.
    # --------------------------------------------------------

    await clear_user_side(
        context,
        target_user_id,
    )

    # --------------------------------------------------------
    # ADMIN CONFIRMATION
    # --------------------------------------------------------

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
    # SILENT.
    # NO RESPONSE.
    # --------------------------------------------------------

    if is_blocked(user.id):
        return

    # --------------------------------------------------------
    # SESSION CHECK
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

        await send_user_message(
            context,
            user.id,
            (
                "⏰ Your chat session has ended.\n\n"
                "If you want to contact the admin again, "
                "send /start."
            ),
        )

        return

    # ========================================================
    # SEND IDENTIFICATION TO ADMIN
    # ========================================================

    try:

        info_message = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=get_user_info(user),
        )

        # Keep admin-side message
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
    # COPY USER MESSAGE TO ADMIN
    # ========================================================

    try:

        copied_message = await message.copy(
            chat_id=ADMIN_ID
        )

        # Keep admin-side message
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

        sent = await message.reply_text(
            "✅ Your message has been sent to the admin."
        )

        # Track user-side bot message
        save_user_message(
            user.id,
            sent.message_id,
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

    # Commands are handled by CommandHandler
    if message.text and message.text.startswith("/"):
        return

    # --------------------------------------------------------
    # Admin must use Telegram's normal Reply
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

    user_id = get_reply_target_user_id(
        message
    )

    if not user_id:

        await message.reply_text(
            "⚠️ I couldn't identify the user for this "
            "message. Please reply to the user's message."
        )

        return

    # --------------------------------------------------------
    # Blocked user
    # --------------------------------------------------------

    if is_blocked(user_id):

        await message.reply_text(
            "🚫 This user is currently blocked."
        )

        return

    # --------------------------------------------------------
    # Session check
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
    # SEND ADMIN REPLY TO USER
    # ========================================================

    try:

        copied = await message.copy(
            chat_id=user_id
        )

        # Track bot-generated message on user side
        save_user_message(
            user_id,
            copied.message_id,
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

async def post_init(
    application: Application,
):
    init_db()

    if application.job_queue:

        # Expire sessions every minute
        application.job_queue.run_repeating(
            expire_sessions,
            interval=60,
            first=10,
        )

        # Delete old ADMIN-SIDE bot messages every 5 minutes
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

    # --------------------------------------------------------
    # USER /start
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # --------------------------------------------------------
    # ADMIN COMMANDS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # ALL OTHER TELEGRAM MESSAGE TYPES
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            forward_user_message,
        )
    )

    # --------------------------------------------------------
    # START BOT
    # --------------------------------------------------------

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
