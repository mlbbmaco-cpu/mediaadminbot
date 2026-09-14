# WALAWWA Anonymous Admin Bot

Bot: `@walawwa_admin_bot`

## What it does

- User sends `/start`.
- A 24-hour session is created.
- Simply opening/starting the bot does NOT notify the admin.
- Only an actual user message is sent to the admin.
- The admin receives the user's Telegram ID and username.
- The actual Telegram message is copied to the admin.
- All normal Telegram message types are accepted.
- The admin uses Telegram's normal Reply feature.
- The bot sends the admin reply to the correct user.
- Sessions automatically expire after 24 hours.
- The user is told when the session ends.
- Admin-side bot messages are automatically deleted after 24 hours.

## Admin

Admin Telegram ID:

```text
8419747522
```

## Northflank environment variables

Add these in Northflank:

```text
BOT_TOKEN=YOUR_BOT_TOKEN
ADMIN_ID=8419747522
SESSION_HOURS=24
DELETE_AFTER_HOURS=24
```

Optional persistent database path:

```text
DB_PATH=/data/anonymous_chat.db
```

## Important: replying

When a user sends a message, the admin receives two messages:

1. User information:

```text
📩 New Anonymous Message

👤 User ID: 123456789
🔹 Username: @example
```

2. The user's actual Telegram message.

For reliable routing, use Telegram's normal **Reply** on the **User information message**. The bot reads the User ID from that message and sends the admin's response to that user.

## Deployment

Connect this repository to Northflank as a worker/service.

The included Procfile runs:

```text
python bot.py
```

Never put `BOT_TOKEN` in GitHub.

## Database persistence

SQLite is included so sessions and cleanup records can survive a process restart.

For sessions to survive a full Northflank container replacement, configure persistent storage and use:

```text
DB_PATH=/data/anonymous_chat.db
```

Without persistent storage, the bot still works, but the SQLite database can be lost when Northflank replaces the container.
