# Discord Translation Bot

A Discord bot that bridges multiple language channels. Messages posted in any registered language channel are automatically translated and forwarded to all other registered channels — appearing as if the **original user** sent them (via Discord Webhooks).

## How It Works

```
A types "你好" in #中文
    ↓
Bot (as A's name + avatar) posts "Hello"      → #english
Bot (as A's name + avatar) posts "こんにちは"  → #日本語
Bot (as A's name + avatar) posts "안녕하세요"  → #한국어

B types "Hello" in #english
    ↓
Bot (as B's name + avatar) posts "你好"        → #中文
Bot (as B's name + avatar) posts "こんにちは"  → #日本語
...
```

## Requirements

- Discord bot with **Message Content Intent** enabled
- Bot permissions: `Send Messages`, `Manage Webhooks`, `Read Message History`

## Setup

### Local

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in DISCORD_TOKEN
python bot.py
```

### Synology DSM Docker (recommended)

1. Copy the project folder to your Synology NAS (e.g. via File Station to `/docker/discord_trans_bot`)

2. Create the `.env` file:
   ```
   DISCORD_TOKEN=your_token_here
   ```

3. Create the data directory:
   ```bash
   mkdir -p data
   ```

4. **Option A — Docker Compose (SSH)**
   ```bash
   docker compose up -d
   ```

5. **Option B — DSM Container Manager UI**
   - Build image: Container Manager → Registry → Build from folder
   - Or pull from your own registry after `docker build -t discord-trans-bot .`
   - Create container with:
     - Environment variable: `DISCORD_TOKEN=...`
     - Volume: `/docker/discord_trans_bot/data` → `/data`
     - Restart policy: Always

The bot stores its channel config in `/data/channel_config.json` — this persists across container restarts.

## Discord Bot Configuration

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create application → Bot → copy token
3. Enable **Message Content Intent** (Bot → Privileged Gateway Intents)
4. OAuth2 → URL Generator:
   - Scopes: `bot`
   - Permissions: `Send Messages`, `Manage Webhooks`, `Read Message History`, `Embed Links`
5. Invite bot to your server

## Commands

| Command | Permission | Description |
|---------|-----------|-------------|
| `!setlang <lang_code> [#channel]` | Manage Channels | Register a channel and auto-create its webhook |
| `!unsetlang [#channel]` | Manage Channels | Remove a channel and delete its webhook |
| `!listlang` | Everyone | List all registered language channels |

## Language Codes (ISO 639-1)

| Code | Language |
|------|----------|
| `zh-TW` | 繁體中文 |
| `zh-CN` | 簡體中文 |
| `en` | English |
| `ja` | 日本語 |
| `ko` | 한국어 |
| `fr` | Français |
| `de` | Deutsch |
| `es` | Español |

Full list: https://py-googletrans.readthedocs.io/en/latest/

## Example Setup

Run once in your server (requires Manage Channels permission):

```
!setlang zh-TW #中文
!setlang en #english
!setlang ja #日本語
!setlang ko #한국어
```

The bot will automatically create a webhook in each channel. From then on, all messages are translated and forwarded instantly.
