# Spike 2: Matrix E2EE Bot

Prove that we can build a reliable Matrix bot with E2EE using matrix-nio.

## What we're testing

1. Bot connects to a Matrix homeserver and logs in
2. E2EE support (encryption enabled)
3. Bot receives messages and echoes them back
4. Bot creates threads programmatically (m.thread relation)
5. Bot reacts to messages with emoji (m.annotation)

## Prerequisites

- A Matrix homeserver (Conduit, Synapse, etc.)
- A bot account on the homeserver
- An E2EE-capable room the bot can join

## Running

```bash
pip install "matrix-nio[e2ee]"

export MATRIX_HOMESERVER="https://matrix.example.com"
export MATRIX_USER="@enclave-bot:example.com"
export MATRIX_PASSWORD="your-password"
export MATRIX_ROOM="#test:example.com"

python bot.py
```

## Interactive Commands

Once the bot is running, send messages in the room:
- Any text → bot echoes it back
- `!thread` → bot creates a thread with multiple messages
- `!react` → bot reacts with ✅ and 🏰
- `!quit` → bot prints results and shuts down

## Success Criteria

- [ ] Bot logs in and syncs
- [ ] E2EE encryption active
- [ ] Bot echoes messages back
- [ ] Bot creates a thread from a message
- [ ] Bot reacts with emoji
