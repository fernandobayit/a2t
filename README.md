# A2T — Telegram Connector for Agent Zero

A2T is a Telegram bridge plugin for [Agent Zero](https://github.com/agent0ai/a0) that enables seamless communication between Telegram users and AI agents.

## Features

- **Multi-bot support** — Run multiple Telegram bots from a single Agent Zero instance
- **Per-user & per-topic project routing** — Route conversations to different projects based on user ID or forum topic
- **Polling & webhook modes** — Choose the connection method that fits your infrastructure
- **Rich message support** — Text, photos, documents, audio, voice, video, animations, polls, and more
- **Markdown → Telegram HTML** — Convert agent responses to Telegram-compatible HTML with tables, code blocks, and inline formatting
- **Inline keyboards** — Send interactive buttons with responses
- **Agent control commands** — `/start`, `/help`, `/clear`, `/status`, `/id`, `/stop`, `/resume`, `/nudge`, `/context`
- **Typing indicators** — Visual feedback while the agent processes messages
- **Group chat support** — Respond to @mentions, replies, or all messages (configurable)
- **Welcome messages** — Greet new group members automatically
- **Attachment handling** — Download and process files, photos, voice messages with size limits
- **WebUI settings panel** — Configure bots from the Agent Zero web interface

## Installation

```bash
# Install via Agent Zero Plugin Hub (when available)
# Or manually:
git clone https://github.com/fernandobayit/a2t.git /a0/usr/plugins/a2t
```

Then restart Agent Zero and configure the plugin in Settings → A2T.

## Configuration

Configure bots via the WebUI settings panel or edit `config.json` directly:

```json
{
  "bots": [
    {
      "name": "my_bot",
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "mode": "polling",
      "allowed_users": ["@yourusername"],
      "group_mode": "mention",
      "default_project": "personal",
      "max_file_size_mb": 20,
      "a0_timeout": 300
    }
  ]
}
```

### Bot Options

| Option | Description | Default |
|---|---|---|
| `name` | Unique bot identifier | (required) |
| `enabled` | Enable/disable the bot | `true` |
| `token` | Telegram Bot token from @BotFather | (required) |
| `mode` | `polling` or `webhook` | `polling` |
| `allowed_users` | User IDs or @usernames (empty = allow all) | `[]` |
| `allowed_chats` | Chat IDs (empty = allow all) | `[]` |
| `group_mode` | `mention`, `all`, or `off` | `mention` |
| `default_project` | Fallback project for routing | `""` |
| `user_projects` | Map user IDs to projects | `{}` |
| `topic_projects` | Map topic/thread IDs to projects | `{}` |
| `max_file_size_mb` | Max attachment size | `20` |
| `a0_timeout` | Agent request timeout (seconds) | `300` |
| `agent_instructions` | Extra instructions for the agent | `""` |

## Commands

| Command | Description |
|---|---|
| `/start` | Start the bot |
| `/help` | Show available commands |
| `/clear` | Start a new conversation |
| `/status` | Show connection status |
| `/id` | Show your User/Chat ID |
| `/stop` | Pause the agent |
| `/resume` | Resume a paused agent |
| `/nudge` | Kick a stuck agent |
| `/context` | Show context window info |

## Architecture

```
a2t/
├── plugin.yaml          # Plugin manifest
├── hooks.py             # Installation hooks
├── helpers/
│   ├── telegram_client.py   # Low-level Telegram API wrapper
│   ├── handler.py            # Message routing, commands, context lifecycle
│   ├── bot_manager.py        # Bot creation, polling/webhook lifecycle
│   ├── dependencies.py       # Auto-install aiogram
│   └── constants.py          # Context keys and paths
├── extensions/
│   └── python/
│       ├── job_loop/             # Bot lifecycle management
│       ├── process_chain_end/    # Auto-reply on chain completion
│       ├── tool_execute_after/   # Response interception & inline updates
│       └── system_prompt/        # Telegram-specific system prompt
├── api/
│   ├── webhook.py           # Webhook endpoint
│   └── test_connection.py   # Token validation endpoint
├── prompts/                 # Prompt templates
└── webui/                   # Settings UI components
```

## Requirements

- [aiogram](https://github.com/aiogram/aiogram) >= 3.15.0
- [aiohttp](https://github.com/aio-libs/aiohttp) >= 3.9.0

Dependencies are auto-installed on first use.

## License

MIT License — see [LICENSE](LICENSE)
