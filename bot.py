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

# msg_id -> cluster dict shared by all messages in a translation group.
# cluster keys:
#   channels       {channel_id: msg_id}
#   contents       {channel_id: translated_text}
#   author         display name of the original sender
#   avatar_url     avatar URL of the original sender (needed for delete+resend)
#   source_ch      channel_id of the original message
#   source_lang    language code of the original channel
#   prefixes       {channel_id: blockquote_prefix_string}  (reply messages only)
#   att_names      {channel_id: [filename, ...]}  for detecting attachment changes
_msg_clusters: dict[int, dict] = {}
_MAX_CLUSTER_ENTRIES = 1500

# Cached pinned message IDs per channel for change detection
_channel_pins: dict[int, set[int]] = {}


def _store_cluster(cluster: dict) -> None:
    for msg_id in cluster["channels"].values():
        _msg_clusters[msg_id] = cluster
    if len(_msg_clusters) > _MAX_CLUSTER_ENTRIES:
        remove_keys = list(_msg_clusters.keys())[: _MAX_CLUSTER_ENTRIES // 3]
        for k in remove_keys:
            del _msg_clusters[k]


def _guild_channels_for(channel_id: int) -> dict:
    for guild in bot.guilds:
        gc = channel_configs.get(guild.id, {})
        if channel_id in gc:
            return gc
    return {}


# ---------------------------------------------------------------------------
# Message events
# ---------------------------------------------------------------------------

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
    attachments = list(message.attachments)
    stickers = [s for s in message.stickers if s.format != discord.StickerFormatType.lottie]

    if not content and not attachments and not stickers:
        return

    username = message.author.display_name
    avatar_url = str(message.author.display_avatar.url)

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
                attachments, stickers, quoted, quoted_author,
            )
        )
        target_channel_ids.append(channel_id)

    if not tasks:
        return

    results = await asyncio.gather(*tasks)

    cluster: dict = {
        "channels": {message.channel.id: message.id},
        "contents": {message.channel.id: content},
        "author": username,
        "avatar_url": avatar_url,
        "source_ch": message.channel.id,
        "source_lang": source_lang,
        "prefixes": {},
        "att_names": {message.channel.id: [a.filename for a in attachments]},
    }
    for ch_id, result in zip(target_channel_ids, results):
        if result is not None:
            sent_id, sent_text = result
            cluster["channels"][ch_id] = sent_id
            cluster["contents"][ch_id] = sent_text or ""
            cluster["att_names"][ch_id] = [a.filename for a in attachments]

    if ref_cluster:
        ref_author = ref_cluster.get("author", "")
        for ch_id in target_channel_ids:
            quoted = ref_cluster["contents"].get(ch_id, "")
            if quoted:
                lines = quoted.splitlines()
                pl: list[str] = []
                if ref_author and lines:
                    pl.append(f"> **{ref_author}**: {lines[0]}")
                    pl.extend(f"> {l}" for l in lines[1:])
                else:
                    pl.extend(f"> {l}" for l in lines)
                cluster["prefixes"][ch_id] = "\n".join(pl)

    _store_cluster(cluster)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    channel = bot.get_channel(payload.channel_id)
    if not channel or not hasattr(channel, "guild"):
        return

    guild_channels = channel_configs.get(channel.guild.id, {})
    if payload.channel_id not in guild_channels:
        return

    cluster = _msg_clusters.get(payload.message_id)
    if not cluster:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.HTTPException):
        return

    if message.author.bot:
        return

    new_content = message.content.strip()
    current_attachments = list(message.attachments)
    current_stickers = [s for s in message.stickers if s.format != discord.StickerFormatType.lottie]

    if not new_content and not current_attachments and not current_stickers:
        return

    source_lang = guild_channels[payload.channel_id]["lang"]

    prev_att_names = cluster.get("att_names", {}).get(payload.channel_id, [])
    curr_att_names = [a.filename for a in current_attachments]
    attachments_changed = prev_att_names != curr_att_names

    edit_targets = [
        (ch_id, msg_id, guild_channels[ch_id]["lang"], guild_channels[ch_id]["webhook_url"])
        for ch_id, msg_id in cluster["channels"].items()
        if ch_id != payload.channel_id
        and ch_id in guild_channels
        and guild_channels[ch_id].get("webhook_url")
    ]

    if attachments_changed:
        # Delete old webhook messages then resend with updated attachments
        await asyncio.gather(*[
            _delete_webhook_message(wh_url, msg_id, ch_id)
            for ch_id, msg_id, _, wh_url in edit_targets
        ])

        send_results = await asyncio.gather(*[
            _translate_and_send(
                new_content, source_lang, lang,
                wh_url, cluster["author"], cluster["avatar_url"],
                current_attachments, current_stickers,
                cluster.get("prefixes", {}).get(ch_id),
                None,
            )
            for ch_id, _, lang, wh_url in edit_targets
        ])

        cluster["contents"][payload.channel_id] = new_content
        cluster["att_names"][payload.channel_id] = curr_att_names
        for (ch_id, old_msg_id, _, _), result in zip(edit_targets, send_results):
            _msg_clusters.pop(old_msg_id, None)
            if result is not None:
                new_msg_id, translated = result
                cluster["channels"][ch_id] = new_msg_id
                cluster["contents"][ch_id] = translated or ""
                cluster["att_names"][ch_id] = curr_att_names
                _msg_clusters[new_msg_id] = cluster
    else:
        edit_results = await asyncio.gather(*[
            _translate_and_edit(new_content, source_lang, lang, wh_url, msg_id, ch_id, cluster)
            for ch_id, msg_id, lang, wh_url in edit_targets
        ])
        cluster["contents"][payload.channel_id] = new_content
        for (ch_id, _, _, _), translated in zip(edit_targets, edit_results):
            if translated:
                cluster["contents"][ch_id] = translated


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    cluster = _msg_clusters.pop(payload.message_id, None)
    if not cluster:
        return

    guild_channels = _guild_channels_for(payload.channel_id)

    await asyncio.gather(*[
        _delete_webhook_message(guild_channels[ch_id]["webhook_url"], msg_id, ch_id)
        for ch_id, msg_id in cluster["channels"].items()
        if msg_id != payload.message_id
        and ch_id in guild_channels
        and guild_channels[ch_id].get("webhook_url")
    ])

    for msg_id in list(cluster["channels"].values()):
        _msg_clusters.pop(msg_id, None)


# ---------------------------------------------------------------------------
# Reaction events
# ---------------------------------------------------------------------------

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    cluster = _msg_clusters.get(payload.message_id)
    if not cluster:
        return

    for channel_id, msg_id in cluster["channels"].items():
        if msg_id == payload.message_id:
            continue
        ch = bot.get_channel(channel_id)
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.add_reaction(payload.emoji)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"Failed to add reaction in channel {channel_id}: {e}")


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    cluster = _msg_clusters.get(payload.message_id)
    if not cluster:
        return

    for channel_id, msg_id in cluster["channels"].items():
        if msg_id == payload.message_id:
            continue
        ch = bot.get_channel(channel_id)
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.remove_reaction(payload.emoji, bot.user)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"Failed to remove reaction in channel {channel_id}: {e}")


@bot.event
async def on_raw_reaction_clear(payload: discord.RawReactionClearEvent):
    cluster = _msg_clusters.get(payload.message_id)
    if not cluster:
        return

    for channel_id, msg_id in cluster["channels"].items():
        if msg_id == payload.message_id:
            continue
        ch = bot.get_channel(channel_id)
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.clear_reactions()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"Failed to clear reactions in channel {channel_id}: {e}")


@bot.event
async def on_raw_reaction_clear_emoji(payload: discord.RawReactionClearEmojiEvent):
    cluster = _msg_clusters.get(payload.message_id)
    if not cluster:
        return

    for channel_id, msg_id in cluster["channels"].items():
        if msg_id == payload.message_id:
            continue
        ch = bot.get_channel(channel_id)
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.clear_reaction(payload.emoji)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"Failed to clear emoji reaction in channel {channel_id}: {e}")


# ---------------------------------------------------------------------------
# Pin events
# ---------------------------------------------------------------------------

@bot.event
async def on_guild_channel_pins_update(channel: discord.abc.GuildChannel, _last_pin):
    if not isinstance(channel, discord.TextChannel):
        return

    guild_channels = channel_configs.get(channel.guild.id, {})
    if channel.id not in guild_channels:
        return

    try:
        pins = await channel.pins()
    except discord.HTTPException:
        return

    current_ids = {m.id for m in pins}
    prev_ids = _channel_pins.get(channel.id, set())
    _channel_pins[channel.id] = current_ids

    newly_pinned = current_ids - prev_ids
    newly_unpinned = prev_ids - current_ids

    for msg_id in newly_pinned:
        cluster = _msg_clusters.get(msg_id)
        if not cluster:
            continue
        for ch_id, cluster_msg_id in cluster["channels"].items():
            if ch_id == channel.id:
                continue
            ch = bot.get_channel(ch_id)
            if not ch:
                continue
            try:
                msg = await ch.fetch_message(cluster_msg_id)
                await msg.pin()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"Failed to pin message {cluster_msg_id} in channel {ch_id}: {e}")

    for msg_id in newly_unpinned:
        cluster = _msg_clusters.get(msg_id)
        if not cluster:
            continue
        for ch_id, cluster_msg_id in cluster["channels"].items():
            if ch_id == channel.id:
                continue
            ch = bot.get_channel(ch_id)
            if not ch:
                continue
            try:
                msg = await ch.fetch_message(cluster_msg_id)
                await msg.unpin()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"Failed to unpin message {cluster_msg_id} in channel {ch_id}: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


async def _translate_and_edit(
    text: str,
    src: str,
    dest: str,
    webhook_url: str,
    msg_id: int,
    ch_id: int,
    cluster: dict,
) -> str | None:
    translated = await asyncio.to_thread(translate_text, text, src, dest)
    if not translated:
        return None

    prefix = cluster.get("prefixes", {}).get(ch_id, "")
    full_content = f"{prefix}\n{translated}" if prefix else translated

    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            await webhook.edit_message(msg_id, content=full_content)
    except Exception as e:
        print(f"Failed to edit webhook message {msg_id} in channel {ch_id}: {e}")
        return None

    return translated


async def _delete_webhook_message(webhook_url: str, msg_id: int, ch_id: int) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            await webhook.delete_message(msg_id)
    except Exception as e:
        print(f"Failed to delete webhook message {msg_id} in channel {ch_id}: {e}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

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
