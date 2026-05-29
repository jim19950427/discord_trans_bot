import os
import io
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

# Tracks translation clusters so replies can reference the correct translated message.
# Any message ID (original or webhook) maps to the same cluster dict:
#   {"channels": {channel_id: msg_id}, "contents": {channel_id: translated_text}}
_msg_clusters: dict[int, dict] = {}
_MAX_CLUSTER_ENTRIES = 1500  # prune when exceeded to keep memory bounded


def _store_cluster(cluster: dict) -> None:
    for msg_id in cluster["channels"].values():
        _msg_clusters[msg_id] = cluster
    if len(_msg_clusters) > _MAX_CLUSTER_ENTRIES:
        remove_keys = list(_msg_clusters.keys())[: _MAX_CLUSTER_ENTRIES // 3]
        for k in remove_keys:
            del _msg_clusters[k]


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
    attachments = message.attachments
    # Only forward stickers that can be rendered as images (skip Lottie JSON format)
    stickers = [s for s in message.stickers if s.format != discord.StickerFormatType.lottie]

    if not content and not attachments and not stickers:
        return

    username = message.author.display_name
    avatar_url = str(message.author.display_avatar.url)

    # Look up quoted content for each target channel when this is a reply
    ref_cluster = None
    if message.reference and message.reference.message_id:
        ref_cluster = _msg_clusters.get(message.reference.message_id)

    tasks = []
    target_channel_ids = []
    for channel_id, info in guild_channels.items():
        if channel_id == message.channel.id:
            continue
        webhook_url = info.get("webhook_url")
        if not webhook_url:
            continue
        target_lang = info["lang"]
        quoted = ref_cluster["contents"].get(channel_id) if ref_cluster else None
        quoted_author = ref_cluster.get("author") if ref_cluster else None
        tasks.append(
            _translate_and_send(
                content, source_lang, target_lang,
                webhook_url, username, avatar_url,
                list(attachments), stickers, quoted, quoted_author,
            )
        )
        target_channel_ids.append(channel_id)

    if not tasks:
        return

    results = await asyncio.gather(*tasks)

    # Build cluster so future replies can reference these messages
    cluster: dict = {
        "channels": {message.channel.id: message.id},
        "contents": {message.channel.id: content},
        "author": username,
    }
    for ch_id, result in zip(target_channel_ids, results):
        if result is not None:
            sent_id, sent_text = result
            cluster["channels"][ch_id] = sent_id
            cluster["contents"][ch_id] = sent_text or ""
    _store_cluster(cluster)


async def _translate_and_send(
    text: str,
    src: str,
    dest: str,
    webhook_url: str,
    username: str,
    avatar_url: str,
    attachments: list,
    stickers: list,
    quoted_content: str | None,
    quoted_author: str | None = None,
) -> tuple[int, str] | None:
    translated: str | None = None
    if text:
        translated = await asyncio.to_thread(translate_text, text, src, dest)

    # Download attachments and stickers so they can be re-uploaded via the webhook
    files: list[discord.File] = []
    urls: list[tuple[str, str]] = (
        [(att.url, att.filename) for att in attachments]
        + [(s.url, f"{s.name}.{'gif' if s.format == discord.StickerFormatType.gif else 'png'}") for s in stickers]
    )
    for url, filename in urls:
        try:
            async with aiohttp.ClientSession() as dl:
                async with dl.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        files.append(discord.File(io.BytesIO(data), filename=filename))
        except Exception as e:
            print(f"Download failed ({filename}): {e}")

    if not translated and not files:
        return None

    # Build message content: optional blockquote for reply context + translation
    parts: list[str] = []
    if quoted_content:
        lines = quoted_content.splitlines()
        if quoted_author and lines:
            parts.append(f"> **{quoted_author}**: {lines[0]}")
            parts.extend(f"> {line}" for line in lines[1:])
        else:
            parts.extend(f"> {line}" for line in lines)
    if translated:
        parts.append(translated)
    final_content = "\n".join(parts) if parts else None

    send_kwargs: dict = {"username": username, "avatar_url": avatar_url, "wait": True}
    if final_content:
        send_kwargs["content"] = final_content
    if files:
        send_kwargs["files"] = files

    async with aiohttp.ClientSession() as session:
        webhook = discord.Webhook.from_url(webhook_url, session=session)
        msg = await webhook.send(**send_kwargs)
        return msg.id, translated or ""


@bot.command(name="setlang")
@commands.has_permissions(manage_channels=True)
async def set_lang(ctx: commands.Context, lang_code: str, channel: discord.TextChannel = None):
    """Register a channel as a language channel. Usage: !setlang <lang_code> [#channel]"""
    target = channel or ctx.channel
    guild_id = ctx.guild.id

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
