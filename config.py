import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# WALAWWA admin Telegram user ID.
ADMIN_ID = 8419747522

# Anonymous chat session length: 24 hours.
SESSION_HOURS = 24

# Bot-generated messages in the admin chat are removed after 24 hours.
DELETE_AFTER_HOURS = 24

# SQLite database path.
DB_PATH = os.getenv("DB_PATH", "anonymous_chat.db")
