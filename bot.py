import os
import asyncio
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
from translator import translate_text, normalize_lang
from config import load_channel_config, save_channel_config

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# guild_id -> {channel_id: {"lang": str, "webhook_url": str}}
channel_configs: dict[int, dict[int, dict]] = {}

WEBHOOK_NAME = "TranslationBot"


@bot.event
async def on_ready():
    global channel_configs
    channel_configs = load_channel_config()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Loaded channel configs for {len(channel_configs)} guild(s)")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    guild_channels = channel_configs.get(message.guild.id, {})
    if message.channel.id not in guild_channels:
        return

    source_info = guild_channels[message.channel.id]
    source_lang = source_info["lang"]
    content = message.content.strip()
    if not content:
        return

    username = message.author.display_name
    avatar_url = str(message.author.display_avatar.url)

    tasks = []
    for channel_id, info in guild_channels.items():
        if channel_id == message.channel.id:
            continue
        target_lang = info["lang"]
        webhook_url = info.get("webhook_url")
        if not webhook_url:
            continue
        tasks.append(_translate_and_send(content, source_lang, target_lang, webhook_url, username, avatar_url))

    if tasks:
        await asyncio.gather(*tasks)


async def _translate_and_send(text: str, src: str, dest: str, webhook_url: str, username: str, avatar_url: str):
    translated = await asyncio.to_thread(translate_text, text, src, dest)
    if not translated:
        return
    async with aiohttp.ClientSession() as session:
        webhook = discord.Webhook.from_url(webhook_url, session=session)
        await webhook.send(content=translated, username=username, avatar_url=avatar_url)


@bot.command(name="setlang")
@commands.has_permissions(manage_channels=True)
async def set_lang(ctx: commands.Context, lang_code: str, channel: discord.TextChannel = None):
    """Register a channel as a language channel. Usage: !setlang <lang_code> [#channel]"""
    target = channel or ctx.channel
    guild_id = ctx.guild.id

    # Find existing TranslationBot webhook or create one
    webhooks = await target.webhooks()
    webhook = next((w for w in webhooks if w.name == WEBHOOK_NAME), None)
    if webhook is None:
        webhook = await target.create_webhook(name=WEBHOOK_NAME)

    if guild_id not in channel_configs:
        channel_configs[guild_id] = {}

    normalized = normalize_lang(lang_code)
    channel_configs[guild_id][target.id] = {
        "lang": normalized,
        "webhook_url": webhook.url,
    }
    save_channel_config(channel_configs)
    await ctx.send(f"Set {target.mention} as the `{normalized}` language channel.")


@bot.command(name="unsetlang")
@commands.has_permissions(manage_channels=True)
async def unset_lang(ctx: commands.Context, channel: discord.TextChannel = None):
    """Remove a channel from language channel list. Usage: !unsetlang [#channel]"""
    target = channel or ctx.channel
    guild_id = ctx.guild.id

    removed = channel_configs.get(guild_id, {}).pop(target.id, None)
    if removed:
        save_channel_config(channel_configs)

        # Clean up the webhook we created
        try:
            webhooks = await target.webhooks()
            for wh in webhooks:
                if wh.name == WEBHOOK_NAME:
                    await wh.delete()
        except discord.Forbidden:
            pass

        await ctx.send(f"Removed {target.mention} from language channels.")
    else:
        await ctx.send(f"{target.mention} was not a registered language channel.")


@bot.command(name="listlang")
async def list_lang(ctx: commands.Context):
    """List all registered language channels in this server."""
    guild_channels = channel_configs.get(ctx.guild.id, {})
    if not guild_channels:
        await ctx.send("No language channels registered. Use `!setlang <lang_code>` to add one.")
        return

    lines = []
    for ch_id, info in guild_channels.items():
        ch = bot.get_channel(ch_id)
        ch_mention = ch.mention if ch else f"(unknown {ch_id})"
        lines.append(f"{ch_mention} → `{info['lang']}`")

    embed = discord.Embed(title="Language Channels", description="\n".join(lines), color=discord.Color.green())
    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(TOKEN)
