# Spike 2: Matrix E2EE Bot

Prove that we can build a reliable Matrix bot with E2EE using matrix-nio.

## What we're testing

1. Bot connects to a Matrix homeserver
2. E2EE works (Olm/Megolm, device verification)
3. Bot receives messages and replies
4. Bot can create threads programmatically
5. Bot can post reactions (for approval flows)

## Prerequisites

- A Matrix homeserver (Conduit recommended, or use matrix.org for testing)
- A bot account on the homeserver
- An E2EE-capable room

## Running

```bash
pip install "matrix-nio[e2ee]"

# Configure
cp ../../config/enclave.example.yaml bot_config.yaml
# Edit bot_config.yaml with your homeserver/credentials

python bot.py --config bot_config.yaml
```

## Success criteria

- [ ] Bot logs in and syncs
- [ ] E2EE works (messages encrypted/decrypted)
- [ ] Bot echoes messages back
- [ ] Bot creates a thread from a message
- [ ] Bot can react with ✅ emoji
