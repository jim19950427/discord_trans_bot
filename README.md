# Discord Translation Bot

A Discord bot that bridges multiple language channels. Messages posted in any registered language channel are automatically translated and forwarded to all other registered channels.

## Features

- Register any text channel as a "language channel" with a language code
- Messages are auto-translated and re-posted to every other language channel via embeds
- Simple slash-style prefix commands (`!setlang`, `!unsetlang`, `!listlang`)
- Config is persisted to `channel_config.json` — survives restarts

## Setup

1. **Clone and install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create a Discord application and bot**
   - Go to https://discord.com/developers/applications
   - Create a new application → Bot → copy the token
   - Enable **Message Content Intent** under Bot → Privileged Gateway Intents

3. **Configure the bot**
   ```bash
   cp .env.example .env
   # Edit .env and paste your bot token
   ```

4. **Invite the bot to your server**
   - OAuth2 → URL Generator → Scopes: `bot` → Permissions: `Send Messages`, `Read Message History`, `Embed Links`

5. **Run the bot**
   ```bash
   python bot.py
   ```

## Commands

| Command | Permission | Description |
|---------|-----------|-------------|
| `!setlang <lang_code> [#channel]` | Manage Channels | Register a channel as a language channel |
| `!unsetlang [#channel]` | Manage Channels | Remove a channel from the language list |
| `!listlang` | Everyone | List all registered language channels |

## Language Codes

Use [ISO 639-1](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes) two-letter codes:

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

In Discord, run these commands (requires Manage Channels permission):

```
!setlang zh-TW #中文
!setlang en #english
!setlang ja #日本語
!setlang ko #한국어
```

Now any message in `#中文` will be automatically translated and posted to `#english`, `#日本語`, and `#한국어` — and vice versa.

## How It Works

```
User sends "你好" in #中文
    ↓
Bot detects the channel is registered as zh-TW
    ↓
Bot translates to en → "Hello"       → posts to #english
Bot translates to ja → "こんにちは"   → posts to #日本語
Bot translates to ko → "안녕하세요"   → posts to #한국어
```
