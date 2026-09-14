import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# WALAWWA admin Telegram user ID.
ADMIN_ID = int(os.getenv("ADMIN_ID", "8419747522"))

# Anonymous chat session length.
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "24"))

# Bot-generated messages in the admin chat are removed after this time.
DELETE_AFTER_HOURS = int(os.getenv("DELETE_AFTER_HOURS", "24"))

# SQLite database path.
DB_PATH = os.getenv("DB_PATH", "anonymous_chat.db")
