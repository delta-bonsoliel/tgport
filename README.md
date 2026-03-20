# tgport

Telegram bot wrapper for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI. Send messages to your Telegram bot and interact with Claude Code remotely.

## Features

- Claude CLI execution via Telegram messages
- Streaming output with real-time message updates
- Session management (conversation context is maintained per chat)
- Access control by Telegram user ID
- Budget and turn limits for safety

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

## Setup

```bash
git clone https://github.com/delta-bonsoliel/tgport.git
cd tgport
python3 -m venv .venv
.venv/bin/pip install -e .
```

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | - | Bot token from BotFather |
| `ALLOWED_USER_IDS` | Yes | - | Comma-separated Telegram user IDs |
| `CLAUDE_WORK_DIR` | No | `~` | Working directory for Claude CLI |
| `CLAUDE_MAX_TURNS` | No | `3` | Max tool-use turns per request |
| `CLAUDE_MAX_BUDGET_USD` | No | `1.0` | Max spend per request (USD) |
| `CLAUDE_SKIP_PERMISSIONS` | No | `false` | Enable `--dangerously-skip-permissions` |
| `EDIT_INTERVAL` | No | `1.5` | Seconds between message updates |
| `RESPONSE_TIMEOUT` | No | `300` | Max seconds to wait for response |

## Usage

```bash
.venv/bin/python -m tgport
```

### Bot Commands

- `/start` - Show help
- `/new` - Start a new conversation (reset session)
- Any text message - Send to Claude

## Security

- Only users listed in `ALLOWED_USER_IDS` can interact with the bot. Unauthorized attempts are logged.
- `CLAUDE_SKIP_PERMISSIONS` is **off by default**. Enabling it allows Claude to execute commands without confirmation. Only use in sandboxed environments with no internet access.
- `CLAUDE_WORK_DIR` defines the scope of file access. Set it to the minimum necessary directory.

## License

MIT
