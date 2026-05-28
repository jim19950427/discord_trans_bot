import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from translator import translate_text
from config import load_channel_config, save_channel_config

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# guild_id -> {channel_id: lang_code}
channel_configs: dict[int, dict[int, str]] = {}


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

    source_lang = guild_channels[message.channel.id]
    content = message.content.strip()
    if not content:
        return

    author_name = message.author.display_name

    tasks = []
    for channel_id, target_lang in guild_channels.items():
        if channel_id == message.channel.id:
            continue
        target_channel = bot.get_channel(channel_id)
        if target_channel is None:
            continue
        tasks.append(_translate_and_send(content, source_lang, target_lang, target_channel, author_name))

    if tasks:
        await asyncio.gather(*tasks)


async def _translate_and_send(text: str, src: str, dest: str, channel: discord.TextChannel, author: str):
    translated = await asyncio.to_thread(translate_text, text, src, dest)
    if translated:
        embed = discord.Embed(description=translated, color=discord.Color.blurple())
        embed.set_footer(text=f"From #{channel.guild.get_channel(_find_src_channel(channel.guild, src)).name} | {author}")
        await channel.send(embed=embed)


def _find_src_channel(guild: discord.Guild, src_lang: str) -> int:
    guild_channels = channel_configs.get(guild.id, {})
    for ch_id, lang in guild_channels.items():
        if lang == src_lang:
            return ch_id
    return 0


@bot.command(name="setlang")
@commands.has_permissions(manage_channels=True)
async def set_lang(ctx: commands.Context, lang_code: str, channel: discord.TextChannel = None):
    """Register a channel as a language channel. Usage: !setlang <lang_code> [#channel]"""
    target = channel or ctx.channel
    guild_id = ctx.guild.id

    if guild_id not in channel_configs:
        channel_configs[guild_id] = {}

    channel_configs[guild_id][target.id] = lang_code.lower()
    save_channel_config(channel_configs)
    await ctx.send(f"Set {target.mention} as the `{lang_code}` language channel.")


@bot.command(name="unsetlang")
@commands.has_permissions(manage_channels=True)
async def unset_lang(ctx: commands.Context, channel: discord.TextChannel = None):
    """Remove a channel from language channel list. Usage: !unsetlang [#channel]"""
    target = channel or ctx.channel
    guild_id = ctx.guild.id

    removed = channel_configs.get(guild_id, {}).pop(target.id, None)
    if removed:
        save_channel_config(channel_configs)
        await ctx.send(f"Removed {target.mention} from language channels.")
    else:
        await ctx.send(f"{target.mention} was not a registered language channel.")


@bot.command(name="listlang")
async def list_lang(ctx: commands.Context):
    """List all registered language channels in this server."""
    guild_channels = channel_configs.get(ctx.guild.id, {})
    if not guild_channels:
        await ctx.send("No language channels registered yet. Use `!setlang <lang_code>` to add one.")
        return

    lines = []
    for ch_id, lang in guild_channels.items():
        ch = bot.get_channel(ch_id)
        ch_mention = ch.mention if ch else f"(unknown channel {ch_id})"
        lines.append(f"{ch_mention} → `{lang}`")

    embed = discord.Embed(title="Language Channels", description="\n".join(lines), color=discord.Color.green())
    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(TOKEN)
