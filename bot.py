import os
import io
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from translator import translate_text, translate_text_nocache, normalize_lang
from config import load_channel_config, save_channel_config
from glossary import (
    load_glossary, save_glossary, get_guild_glossary,
    load_substitutions, save_substitutions, get_guild_substitutions,
    load_user_langs, save_user_langs,
)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# guild_id -> {channel_id: {"lang": str, "webhook_url": str}}
channel_configs: dict[int, dict[int, dict]] = {}

# {str(guild_id): {source_term: {target_lang: translation}}}
_glossary_data: dict = {}

# {str(guild_id): {source_term: replacement}}
_substitutions_data: dict = {}

# {str(user_id): [lang_code, ...]}
_user_langs_data: dict = {}

WEBHOOK_NAME = "TranslationBot"
NO_TRANSLATE_PREFIX = "//"
RAW_FORWARD_PREFIX = "\\"
FEEDBACK_EMOJI = "🔄"

# msg_id -> cluster dict shared by all messages in a translation group
# cluster keys:
#   channels       {channel_id: msg_id}
#   contents       {channel_id: translated_text}
#   author         display name of the original sender
#   avatar_url     avatar URL (needed for delete+resend on attachment edit)
#   source_ch      channel_id of the original message
#   source_lang    language code of the original channel
#   prefixes       {channel_id: blockquote_prefix_string}  (reply messages only)
#   att_names      {channel_id: [filename, ...]}  for detecting attachment changes
#   embed_count    number of embeds seen so far (for link-preview forwarding)
_msg_clusters: dict[int, dict] = {}
_MAX_CLUSTER_ENTRIES = 1500

# Cached pinned message ID sets per channel for change detection
_channel_pins: dict[int, set[int]] = {}

# thread_id -> {parent_ch_id: thread_id, ...} mapping across all language channels
_thread_clusters: dict[int, dict[int, int]] = {}


def _store_cluster(cluster: dict) -> None:
    for msg_id in cluster["channels"].values():
        _msg_clusters[msg_id] = cluster
    if len(_msg_clusters) > _MAX_CLUSTER_ENTRIES:
        remove_keys = list(_msg_clusters.keys())[: _MAX_CLUSTER_ENTRIES // 3]
        for k in remove_keys:
            del _msg_clusters[k]


def _group_channels(guild_channels: dict, channel_id: int) -> dict:
    """Return only the channels in the same group as channel_id."""
    my_group = guild_channels[channel_id].get("group", "default")
    return {cid: info for cid, info in guild_channels.items()
            if info.get("group", "default") == my_group}


def _guild_channels_for(channel_id: int) -> dict:
    """Return group-filtered channels for channel_id (supports thread channel IDs)."""
    for guild in bot.guilds:
        gc = channel_configs.get(guild.id, {})
        if channel_id in gc:
            return _group_channels(gc, channel_id)
    # channel_id might be a thread — try its parent
    ch = bot.get_channel(channel_id)
    if isinstance(ch, discord.Thread) and ch.parent_id is not None:
        for guild in bot.guilds:
            gc = channel_configs.get(guild.id, {})
            if ch.parent_id in gc:
                return _group_channels(gc, ch.parent_id)
    return {}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    global channel_configs, _glossary_data, _substitutions_data, _user_langs_data
    channel_configs = load_channel_config()
    _glossary_data = load_glossary()
    _substitutions_data = load_substitutions()
    _user_langs_data = load_user_langs()

    # Pre-populate pin cache so the first pin event doesn't treat all existing
    # pins as newly added (which would cause spurious sync attempts).
    for gc in channel_configs.values():
        for ch_id in gc:
            ch = bot.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    _channel_pins[ch_id] = {m.id async for m in ch.pins()}
                except discord.HTTPException:
                    _channel_pins[ch_id] = set()

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Loaded channel configs for {len(channel_configs)} guild(s)")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")


# ---------------------------------------------------------------------------
# Message events
# ---------------------------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    all_gc = channel_configs.get(message.guild.id, {})

    # Resolve thread vs normal channel
    thread_map: dict[int, int] | None = None
    if isinstance(message.channel, discord.Thread):
        thread_map = _thread_clusters.get(message.channel.id)
        if thread_map is None:
            return  # untracked thread
        parent_ch_id = message.channel.parent_id
        if parent_ch_id not in all_gc:
            return
        guild_channels = _group_channels(all_gc, parent_ch_id)
        source_ch_id = parent_ch_id
    else:
        if message.channel.id not in all_gc:
            return
        guild_channels = _group_channels(all_gc, message.channel.id)
        source_ch_id = message.channel.id

    content = message.content.strip()

    # // prefix: skip translation, message stays only in source channel
    if content.startswith(NO_TRANSLATE_PREFIX):
        return

    attachments = list(message.attachments)
    stickers = [s for s in message.stickers if s.format != discord.StickerFormatType.lottie]

    # \ prefix: forward original text as-is to all channels, no translation
    raw_forward = content.startswith(RAW_FORWARD_PREFIX)
    if raw_forward:
        content = content[len(RAW_FORWARD_PREFIX):].lstrip()

    if not content and not attachments and not stickers:
        return

    source_lang = guild_channels[source_ch_id]["lang"]
    username = message.author.display_name
    avatar_url = str(message.author.display_avatar.url)
    guild_glossary = get_guild_glossary(message.guild.id, _glossary_data)
    guild_substitutions = get_guild_substitutions(message.guild.id, _substitutions_data)

    ref_cluster = None
    if message.reference and message.reference.message_id:
        ref_cluster = _msg_clusters.get(message.reference.message_id)

    tasks = []
    target_channel_ids = []
    target_thread_ids: list[int | None] = []
    for channel_id, info in guild_channels.items():
        if channel_id == source_ch_id:
            continue
        webhook_url = info.get("webhook_url")
        if not webhook_url:
            continue
        # For thread messages, route to the matching thread in each target channel
        target_thread_id: int | None = None
        if thread_map is not None:
            target_thread_id = thread_map.get(channel_id)
            if target_thread_id is None:
                continue  # no corresponding thread in this channel
        target_lang = info["lang"]
        quoted = ref_cluster["contents"].get(channel_id) if ref_cluster else None
        quoted_author = ref_cluster.get("author") if ref_cluster else None
        tasks.append(
            _raw_forward_send(
                content, webhook_url, username, avatar_url, attachments, stickers,
                quoted, quoted_author, target_thread_id,
            ) if raw_forward else
            _translate_and_send(
                content, source_lang, target_lang,
                webhook_url, username, avatar_url,
                attachments, stickers, quoted, quoted_author,
                guild_glossary, guild_substitutions, target_thread_id,
            )
        )
        target_channel_ids.append(channel_id)
        target_thread_ids.append(target_thread_id)

    if not tasks:
        return

    results = await asyncio.gather(*tasks)

    cluster: dict = {
        "channels": {source_ch_id: message.id},
        "thread_channels": ({source_ch_id: message.channel.id} if thread_map else {}),
        "contents": {source_ch_id: content},
        "author": username,
        "avatar_url": avatar_url,
        "source_ch": source_ch_id,
        "source_lang": source_lang,
        "prefixes": {},
        "att_names": {source_ch_id: [a.filename for a in attachments]},
        "embed_count": len(message.embeds),
    }
    for ch_id, tid, result in zip(target_channel_ids, target_thread_ids, results):
        if result is not None:
            sent_id, sent_text = result
            cluster["channels"][ch_id] = sent_id
            cluster["contents"][ch_id] = sent_text or ""
            cluster["att_names"][ch_id] = [a.filename for a in attachments]
            if tid is not None:
                cluster["thread_channels"][ch_id] = tid

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

    # Schedule a delayed retry for channels where translation failed
    # (sent text equals original source text — translation fell back to original)
    if not raw_forward and content:
        for ch_id, result in zip(target_channel_ids, results):
            if result is None:
                continue
            sent_id, sent_text = result
            if sent_text.strip() == content.strip():
                asyncio.create_task(
                    _retry_translate(
                        content, source_lang,
                        guild_channels[ch_id]["lang"],
                        guild_channels[ch_id]["webhook_url"],
                        sent_id, ch_id, cluster, guild_glossary,
                    )
                )


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    channel = bot.get_channel(payload.channel_id)
    if not channel or not hasattr(channel, "guild"):
        return

    all_gc = channel_configs.get(channel.guild.id, {})
    # Resolve thread vs TextChannel for the source
    source_ch_id = payload.channel_id
    if isinstance(channel, discord.Thread):
        source_ch_id = channel.parent_id
    if source_ch_id not in all_gc:
        return
    guild_channels = _group_channels(all_gc, source_ch_id)

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
    current_embeds = message.embeds

    source_lang = guild_channels[source_ch_id]["lang"]
    guild_glossary = get_guild_glossary(channel.guild.id, _glossary_data)
    guild_substitutions = get_guild_substitutions(channel.guild.id, _substitutions_data)

    edit_targets = [
        (ch_id, msg_id, guild_channels[ch_id]["lang"], guild_channels[ch_id]["webhook_url"])
        for ch_id, msg_id in cluster["channels"].items()
        if ch_id != source_ch_id
        and ch_id in guild_channels
        and guild_channels[ch_id].get("webhook_url")
    ]

    # --- Embed forwarding (Discord adds link previews asynchronously) ---
    stored_embed_count = cluster.get("embed_count", 0)
    if len(current_embeds) > stored_embed_count:
        cluster["embed_count"] = len(current_embeds)
        for ch_id, msg_id, _, wh_url in edit_targets:
            try:
                async with aiohttp.ClientSession() as session:
                    webhook = discord.Webhook.from_url(wh_url, session=session)
                    await webhook.edit_message(msg_id, embeds=current_embeds)
            except Exception as e:
                print(f"Failed to forward embeds to channel {ch_id}: {e}")

    # --- Text / attachment edit ---
    if not new_content and not current_attachments and not current_stickers:
        return

    prev_att_names = cluster.get("att_names", {}).get(payload.channel_id, [])
    curr_att_names = [a.filename for a in current_attachments]
    attachments_changed = prev_att_names != curr_att_names

    if attachments_changed:
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
                None, guild_glossary, guild_substitutions,
            )
            for ch_id, _, lang, wh_url in edit_targets
        ])

        cluster["contents"][source_ch_id] = new_content
        cluster["att_names"][source_ch_id] = curr_att_names
        for (ch_id, old_msg_id, _, _), result in zip(edit_targets, send_results):
            _msg_clusters.pop(old_msg_id, None)
            if result is not None:
                new_msg_id, translated = result
                cluster["channels"][ch_id] = new_msg_id
                cluster["contents"][ch_id] = translated or ""
                cluster["att_names"][ch_id] = curr_att_names
                _msg_clusters[new_msg_id] = cluster
    elif new_content:
        edit_results = await asyncio.gather(*[
            _translate_and_edit(new_content, source_lang, lang, wh_url, msg_id, ch_id, cluster, guild_glossary, guild_substitutions)
            for ch_id, msg_id, lang, wh_url in edit_targets
        ])
        cluster["contents"][source_ch_id] = new_content
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


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    guild_channels = _guild_channels_for(payload.channel_id)
    tasks: list = []
    seen_clusters: set[int] = set()

    for msg_id in payload.message_ids:
        cluster = _msg_clusters.pop(msg_id, None)
        if not cluster:
            continue
        cluster_key = id(cluster)
        if cluster_key in seen_clusters:
            continue
        seen_clusters.add(cluster_key)

        for ch_id, cluster_msg_id in cluster["channels"].items():
            if cluster_msg_id in payload.message_ids:
                continue
            info = guild_channels.get(ch_id)
            if not info or not info.get("webhook_url"):
                continue
            tasks.append(_delete_webhook_message(info["webhook_url"], cluster_msg_id, ch_id))

        for mid in list(cluster["channels"].values()):
            _msg_clusters.pop(mid, None)

    if tasks:
        await asyncio.gather(*tasks)


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

    # Translation feedback: re-translate and edit the message
    if str(payload.emoji) == FEEDBACK_EMOJI:
        await _handle_feedback(payload, cluster)
        return  # don't sync this reaction to other channels

    thread_channels = cluster.get("thread_channels", {})
    for channel_id, msg_id in cluster["channels"].items():
        if msg_id == payload.message_id:
            continue
        actual_ch_id = thread_channels.get(channel_id, channel_id)
        ch = bot.get_channel(actual_ch_id)
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
    thread_channels = cluster.get("thread_channels", {})
    for channel_id, msg_id in cluster["channels"].items():
        if msg_id == payload.message_id:
            continue
        actual_ch_id = thread_channels.get(channel_id, channel_id)
        ch = bot.get_channel(actual_ch_id)
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
    thread_channels = cluster.get("thread_channels", {})
    for channel_id, msg_id in cluster["channels"].items():
        if msg_id == payload.message_id:
            continue
        actual_ch_id = thread_channels.get(channel_id, channel_id)
        ch = bot.get_channel(actual_ch_id)
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
    thread_channels = cluster.get("thread_channels", {})
    for channel_id, msg_id in cluster["channels"].items():
        if msg_id == payload.message_id:
            continue
        actual_ch_id = thread_channels.get(channel_id, channel_id)
        ch = bot.get_channel(actual_ch_id)
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
        current_ids = {m.id async for m in channel.pins()}
    except discord.HTTPException:
        return
    prev_ids = _channel_pins.get(channel.id, set())
    _channel_pins[channel.id] = current_ids

    for msg_id in current_ids - prev_ids:
        cluster = _msg_clusters.get(msg_id)
        if not cluster:
            continue
        for ch_id, cluster_msg_id in cluster["channels"].items():
            if ch_id == channel.id:
                continue
            if cluster_msg_id in _channel_pins.get(ch_id, set()):
                continue  # already pinned — skip to avoid cascade re-pinning
            ch = bot.get_channel(ch_id)
            if not ch:
                continue
            try:
                await (await ch.fetch_message(cluster_msg_id)).pin()
                _channel_pins.setdefault(ch_id, set()).add(cluster_msg_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"Failed to pin {cluster_msg_id} in channel {ch_id}: {e}")

    for msg_id in prev_ids - current_ids:
        cluster = _msg_clusters.get(msg_id)
        if not cluster:
            continue
        for ch_id, cluster_msg_id in cluster["channels"].items():
            if ch_id == channel.id:
                continue
            if cluster_msg_id not in _channel_pins.get(ch_id, set()):
                continue  # not pinned there — skip to avoid cascade
            ch = bot.get_channel(ch_id)
            if not ch:
                continue
            try:
                await (await ch.fetch_message(cluster_msg_id)).unpin()
                _channel_pins.get(ch_id, set()).discard(cluster_msg_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"Failed to unpin {cluster_msg_id} in channel {ch_id}: {e}")


# ---------------------------------------------------------------------------
# Thread events
# ---------------------------------------------------------------------------

@bot.event
async def on_thread_create(thread: discord.Thread):
    if not thread.guild:
        return
    # Skip threads created by the bot itself to prevent cascade
    if thread.owner_id == bot.user.id:
        return
    all_gc = channel_configs.get(thread.guild.id, {})
    if thread.parent_id not in all_gc:
        return
    gc = _group_channels(all_gc, thread.parent_id)
    source_lang = gc[thread.parent_id]["lang"]
    thread_map: dict[int, int] = {thread.parent_id: thread.id}

    for ch_id, info in gc.items():
        if ch_id == thread.parent_id:
            continue
        target_ch = bot.get_channel(ch_id)
        if not isinstance(target_ch, discord.TextChannel):
            continue
        target_lang = info["lang"]
        translated_name = await asyncio.to_thread(
            translate_text, thread.name, source_lang, target_lang
        ) or thread.name
        try:
            new_thread = await target_ch.create_thread(
                name=translated_name[:100],
                type=discord.ChannelType.public_thread,
            )
            thread_map[ch_id] = new_thread.id
        except Exception as e:
            print(f"[thread] failed to create in ch={ch_id}: {e}")

    # Bidirectional index so any thread_id can look up the full mapping
    for tid in thread_map.values():
        _thread_clusters[tid] = thread_map


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _retry_translate(
    text: str,
    src: str,
    dest: str,
    webhook_url: str,
    msg_id: int,
    ch_id: int,
    cluster: dict,
    glossary: dict | None = None,
    delay: int = 10,
) -> None:
    await asyncio.sleep(delay)
    translated = await asyncio.to_thread(translate_text, text, src, dest, glossary or {})
    if not translated or translated.strip() == text.strip():
        print(f"[retry] still failed ({src}->{dest}): {repr(text)}")
        return
    prefix = cluster.get("prefixes", {}).get(ch_id, "")
    full_content = f"{prefix}\n{translated}" if prefix else translated
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            await webhook.edit_message(msg_id, content=full_content)
        cluster["contents"][ch_id] = translated
        print(f"[retry] updated ({src}->{dest}): {repr(translated)}")
    except Exception as e:
        print(f"[retry] edit failed msg={msg_id} ch={ch_id}: {e}")


async def _raw_forward_send(
    text: str,
    webhook_url: str,
    username: str,
    avatar_url: str,
    attachments: list,
    stickers: list,
    quoted_content: str | None,
    quoted_author: str | None = None,
    thread_id: int | None = None,
) -> tuple[int, str] | None:
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

    if not text and not files:
        return None

    parts: list[str] = []
    if quoted_content:
        lines = quoted_content.splitlines()
        if quoted_author and lines:
            parts.append(f"> **{quoted_author}**: {lines[0]}")
            parts.extend(f"> {line}" for line in lines[1:])
        else:
            parts.extend(f"> {line}" for line in lines)
    if text:
        parts.append(text)
    final_content = "\n".join(parts) if parts else None

    send_kwargs: dict = {"username": username, "avatar_url": avatar_url, "wait": True}
    if final_content:
        send_kwargs["content"] = final_content
    if files:
        send_kwargs["files"] = files
    if thread_id:
        send_kwargs["thread"] = discord.Object(id=thread_id)

    async with aiohttp.ClientSession() as session:
        webhook = discord.Webhook.from_url(webhook_url, session=session)
        msg = await webhook.send(**send_kwargs)
        return msg.id, text or ""


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
    glossary: dict | None = None,
    substitutions: dict | None = None,
    thread_id: int | None = None,
) -> tuple[int, str] | None:
    translated: str | None = None
    if text:
        translated = await asyncio.to_thread(translate_text, text, src, dest, glossary or {}, substitutions or {})

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
    if thread_id:
        send_kwargs["thread"] = discord.Object(id=thread_id)

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
    glossary: dict | None = None,
    substitutions: dict | None = None,
) -> str | None:
    translated = await asyncio.to_thread(translate_text, text, src, dest, glossary or {}, substitutions or {})
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


async def _do_retranslate(parent_ch_id: int, msg_id: int, cluster: dict, guild_id: int) -> str | None:
    """Re-translate a specific message and edit it in place. Returns new text or None."""
    guild_channels = _guild_channels_for(parent_ch_id)
    if parent_ch_id not in guild_channels:
        return None
    info = guild_channels[parent_ch_id]
    source_ch = cluster["source_ch"]
    source_text = cluster["contents"].get(source_ch, "")
    source_lang = cluster["source_lang"]
    target_lang = info["lang"]
    if source_lang == target_lang or not source_text:
        return None
    webhook_url = info.get("webhook_url")
    if not webhook_url:
        return None

    guild_glossary = get_guild_glossary(guild_id, _glossary_data)
    guild_subs = get_guild_substitutions(guild_id, _substitutions_data)
    translated = await asyncio.to_thread(
        translate_text_nocache, source_text, source_lang, target_lang, guild_glossary, guild_subs
    )
    if not translated or translated.strip() == source_text.strip():
        return None

    prefix = cluster.get("prefixes", {}).get(parent_ch_id, "")
    full_content = f"{prefix}\n{translated}" if prefix else translated
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            await webhook.edit_message(msg_id, content=full_content)
        cluster["contents"][parent_ch_id] = translated
        print(f"[retranslate] ({source_lang}->{target_lang}) updated msg={msg_id}")
        return translated
    except Exception as e:
        print(f"[retranslate] edit failed msg={msg_id}: {e}")
        return None


async def _handle_feedback(payload: discord.RawReactionActionEvent, cluster: dict) -> None:
    """Re-translate the message for the channel where 🔄 was reacted."""
    react_ch_id = payload.channel_id
    thread_channels = cluster.get("thread_channels", {})
    parent_ch_id = react_ch_id
    if thread_channels:
        for pid, tid in thread_channels.items():
            if tid == react_ch_id:
                parent_ch_id = pid
                break

    guild_id: int | None = None
    for guild in bot.guilds:
        if parent_ch_id in channel_configs.get(guild.id, {}):
            guild_id = guild.id
            break
    if guild_id is None:
        return

    msg_id = cluster["channels"].get(parent_ch_id)
    if not msg_id:
        return

    translated = await _do_retranslate(parent_ch_id, msg_id, cluster, guild_id)
    if not translated:
        return

    # Remove the 🔄 reaction so user can trigger again if needed
    actual_ch_id = thread_channels.get(parent_ch_id, parent_ch_id)
    ch = bot.get_channel(actual_ch_id)
    if ch:
        try:
            msg = await ch.fetch_message(msg_id)
            await msg.remove_reaction(FEEDBACK_EMOJI, discord.Object(id=payload.user_id))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Prefix commands (legacy / backwards-compat)
# ---------------------------------------------------------------------------

@bot.command(name="addlang")
@commands.has_permissions(manage_channels=True)
async def prefix_setlang(ctx: commands.Context, lang_code: str, channel: discord.TextChannel = None, group: str = "default"):
    await _do_setlang(ctx.guild.id, channel or ctx.channel, lang_code, ctx.send, group)


@bot.command(name="removelang")
@commands.has_permissions(manage_channels=True)
async def prefix_unsetlang(ctx: commands.Context, channel: discord.TextChannel = None):
    await _do_unsetlang(ctx.guild.id, channel or ctx.channel, ctx.send)


@bot.command(name="listlang")
async def prefix_listlang(ctx: commands.Context):
    await _do_listlang(ctx.guild.id, ctx.send)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="addlang", description="設定語言頻道")
@app_commands.describe(
    lang_code="語言代碼（例如 zh-TW, en, ja, ko）",
    channel="目標頻道（留空表示目前頻道）",
    group="頻道群組名稱（留空為 default；同群組頻道互相翻譯）",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_addlang(
    interaction: discord.Interaction,
    lang_code: str,
    channel: discord.TextChannel = None,
    group: str = "default",
):
    await _do_setlang(
        interaction.guild_id,
        channel or interaction.channel,
        lang_code,
        interaction.response.send_message,
        group,
    )


@bot.tree.command(name="removelang", description="取消語言頻道設定")
@app_commands.describe(channel="目標頻道（留空表示目前頻道）")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_removelang(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
):
    await _do_unsetlang(
        interaction.guild_id,
        channel or interaction.channel,
        interaction.response.send_message,
    )


@bot.tree.command(name="listlang", description="列出所有語言頻道")
async def slash_listlang(interaction: discord.Interaction):
    await _do_listlang(interaction.guild_id, interaction.response.send_message)


@bot.tree.command(name="addterm", description="新增詞彙表條目")
@app_commands.describe(
    word="來源詞彙",
    lang="目標語言代碼（例如 en, ja）",
    translation="對應翻譯",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_addterm(
    interaction: discord.Interaction,
    word: str,
    lang: str,
    translation: str,
):
    guild_id = str(interaction.guild_id)
    if guild_id not in _glossary_data:
        _glossary_data[guild_id] = {}
    if word not in _glossary_data[guild_id]:
        _glossary_data[guild_id][word] = {}
    normalized = normalize_lang(lang)
    _glossary_data[guild_id][word][normalized] = translation
    save_glossary(_glossary_data)
    await interaction.response.send_message(
        f"已新增詞彙：`{word}` → `{translation}` （{normalized}）", ephemeral=True
    )


@bot.tree.command(name="removeterm", description="移除詞彙表條目")
@app_commands.describe(
    word="來源詞彙",
    lang="目標語言代碼（留空則刪除該詞彙所有語言的翻譯）",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_removeterm(
    interaction: discord.Interaction,
    word: str,
    lang: str = None,
):
    guild_id = str(interaction.guild_id)
    guild_terms = _glossary_data.get(guild_id, {})
    if word not in guild_terms:
        await interaction.response.send_message(f"找不到詞彙 `{word}`。", ephemeral=True)
        return
    if lang:
        normalized = normalize_lang(lang)
        guild_terms[word].pop(normalized, None)
        if not guild_terms[word]:
            del guild_terms[word]
        msg = f"已移除詞彙：`{word}` 的 {normalized} 翻譯。"
    else:
        del guild_terms[word]
        msg = f"已移除詞彙：`{word}` 所有語言的翻譯。"
    save_glossary(_glossary_data)
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="addproper", description="新增專有名詞（在所有語言頻道保留原文，不翻譯）")
@app_commands.describe(word="要保留原文的詞彙，例如人名 Jim、伺服器名稱等")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_addproper(interaction: discord.Interaction, word: str):
    guild_id = str(interaction.guild_id)
    if guild_id not in _glossary_data:
        _glossary_data[guild_id] = {}
    _glossary_data[guild_id][word] = {"*": word}
    save_glossary(_glossary_data)
    await interaction.response.send_message(
        f"已新增專有名詞：`{word}`（所有語言頻道皆保留原文）", ephemeral=True
    )


@bot.tree.command(name="removeproper", description="移除專有名詞")
@app_commands.describe(word="要移除的專有名詞")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_removeproper(interaction: discord.Interaction, word: str):
    guild_id = str(interaction.guild_id)
    guild_terms = _glossary_data.get(guild_id, {})
    entry = guild_terms.get(word)
    if not entry or "*" not in entry:
        await interaction.response.send_message(f"找不到專有名詞 `{word}`。", ephemeral=True)
        return
    del guild_terms[word]
    save_glossary(_glossary_data)
    await interaction.response.send_message(f"已移除專有名詞：`{word}`。", ephemeral=True)


@bot.tree.command(name="listterms", description="列出所有詞彙表條目")
async def slash_listterms(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    terms = _glossary_data.get(guild_id, {})
    if not terms:
        await interaction.response.send_message("詞彙表目前是空的。", ephemeral=True)
        return

    proper_lines: list[str] = []
    term_lines: list[str] = []
    for word, translations in terms.items():
        if "*" in translations:
            proper_lines.append(f"`{word}`")
        else:
            pairs = "、".join(f"{lang}: {t}" for lang, t in translations.items())
            term_lines.append(f"`{word}` → {pairs}")

    embed = discord.Embed(title="詞彙表", color=discord.Color.blue())
    if proper_lines:
        embed.add_field(name="專有名詞（不翻譯）", value="\n".join(proper_lines), inline=False)
    if term_lines:
        embed.add_field(name="翻譯詞彙", value="\n".join(term_lines), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="addsub", description="新增翻譯前替換規則（來源文字替換後再翻譯）")
@app_commands.describe(
    word="要被替換的來源文字",
    replacement="替換成的文字",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_addsub(interaction: discord.Interaction, word: str, replacement: str):
    guild_id = str(interaction.guild_id)
    if guild_id not in _substitutions_data:
        _substitutions_data[guild_id] = {}
    _substitutions_data[guild_id][word] = replacement
    save_substitutions(_substitutions_data)
    await interaction.response.send_message(
        f"已新增替換規則：`{word}` → `{replacement}`（翻譯前替換）", ephemeral=True
    )


@bot.tree.command(name="removesub", description="移除翻譯前替換規則")
@app_commands.describe(word="要移除的來源文字")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_removesub(interaction: discord.Interaction, word: str):
    guild_id = str(interaction.guild_id)
    guild_subs = _substitutions_data.get(guild_id, {})
    if word not in guild_subs:
        await interaction.response.send_message(f"找不到替換規則 `{word}`。", ephemeral=True)
        return
    del guild_subs[word]
    save_substitutions(_substitutions_data)
    await interaction.response.send_message(f"已移除替換規則：`{word}`。", ephemeral=True)


@bot.tree.command(name="listsubs", description="列出所有翻譯前替換規則")
async def slash_listsubs(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    subs = _substitutions_data.get(guild_id, {})
    if not subs:
        await interaction.response.send_message("目前沒有替換規則。", ephemeral=True)
        return
    lines = [f"`{word}` → `{rep}`" for word, rep in subs.items()]
    embed = discord.Embed(
        title="翻譯前替換規則",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="addmylang", description="新增個人翻譯語言（右鍵翻譯時使用）")
@app_commands.describe(lang="語言代碼（例如 zh-TW, en, ja）")
async def slash_addmylang(interaction: discord.Interaction, lang: str):
    user_id = str(interaction.user.id)
    normalized = normalize_lang(lang)
    langs = _user_langs_data.setdefault(user_id, [])
    if normalized in langs:
        await interaction.response.send_message(
            f"`{normalized}` 已在你的翻譯語言清單中。", ephemeral=True
        )
        return
    langs.append(normalized)
    save_user_langs(_user_langs_data)
    await interaction.response.send_message(
        f"已新增 `{normalized}` 到你的翻譯語言。目前清單：{', '.join(f'`{l}`' for l in langs)}",
        ephemeral=True,
    )


@bot.tree.command(name="removemylang", description="移除個人翻譯語言")
@app_commands.describe(lang="要移除的語言代碼")
async def slash_removemylang(interaction: discord.Interaction, lang: str):
    user_id = str(interaction.user.id)
    normalized = normalize_lang(lang)
    langs = _user_langs_data.get(user_id, [])
    if normalized not in langs:
        await interaction.response.send_message(
            f"找不到 `{normalized}`，請先用 `/listmylang` 確認目前清單。", ephemeral=True
        )
        return
    langs.remove(normalized)
    if not langs:
        del _user_langs_data[user_id]
    save_user_langs(_user_langs_data)
    remaining = f"目前清單：{', '.join(f'`{l}`' for l in langs)}" if langs else "目前清單為空。"
    await interaction.response.send_message(f"已移除 `{normalized}`。{remaining}", ephemeral=True)


@bot.tree.command(name="listmylang", description="列出你目前設定的個人翻譯語言")
async def slash_listmylang(interaction: discord.Interaction):
    langs = _user_langs_data.get(str(interaction.user.id), [])
    if not langs:
        await interaction.response.send_message(
            "你尚未設定任何翻譯語言。使用 `/addmylang` 新增。", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"你的翻譯語言：{', '.join(f'`{l}`' for l in langs)}", ephemeral=True
    )


@bot.tree.context_menu(name="查看原文")
async def view_source_context_menu(interaction: discord.Interaction, message: discord.Message):
    cluster = _msg_clusters.get(message.id)
    if not cluster:
        await interaction.response.send_message(
            "找不到此訊息的原文記錄。（僅追蹤容器重啟後發送的訊息）",
            ephemeral=True,
        )
        return

    source_ch_id = cluster["source_ch"]
    source_text = cluster["contents"].get(source_ch_id, "")
    source_lang = cluster["source_lang"]
    author = cluster.get("author", "")

    source_ch = bot.get_channel(source_ch_id)
    ch_mention = source_ch.mention if source_ch else f"(#{source_ch_id})"

    embed = discord.Embed(title="原文", color=discord.Color.greyple())
    embed.add_field(name=f"來源頻道（{source_lang}）", value=ch_mention, inline=True)
    if author:
        embed.add_field(name="發送者", value=author, inline=True)
    embed.add_field(name="原文內容", value=(source_text[:1024] or "（無文字）"), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.context_menu(name="重新翻譯")
async def retranslate_context_menu(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)

    cluster = _msg_clusters.get(message.id)
    if not cluster:
        await interaction.followup.send(
            "找不到此訊息的翻譯記錄。（僅追蹤容器重啟後發送的訊息）",
            ephemeral=True,
        )
        return

    # Find parent_ch_id for this message in the cluster
    parent_ch_id: int | None = None
    for pid, mid in cluster["channels"].items():
        if mid == message.id:
            parent_ch_id = pid
            break
    if parent_ch_id is None:
        await interaction.followup.send("無法找到此訊息對應的頻道。", ephemeral=True)
        return

    if parent_ch_id == cluster["source_ch"]:
        await interaction.followup.send("此訊息是原文，無法重新翻譯。", ephemeral=True)
        return

    guild_id: int | None = None
    for guild in bot.guilds:
        if parent_ch_id in channel_configs.get(guild.id, {}):
            guild_id = guild.id
            break
    if guild_id is None:
        await interaction.followup.send("找不到對應的伺服器設定。", ephemeral=True)
        return

    translated = await _do_retranslate(parent_ch_id, message.id, cluster, guild_id)
    if translated:
        await interaction.followup.send("已重新翻譯。", ephemeral=True)
    else:
        await interaction.followup.send("重新翻譯失敗，或翻譯結果與原文相同。", ephemeral=True)


@bot.tree.context_menu(name="翻譯此訊息")
async def translate_context_menu(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)

    text = message.content
    if not text:
        await interaction.followup.send("此訊息沒有文字內容。\nThis message has no text content.", ephemeral=True)
        return

    # Check user's registered languages
    user_langs = _user_langs_data.get(str(interaction.user.id), [])
    if not user_langs:
        await interaction.followup.send(
            "## 尚未設定翻譯語言\n"
            "請使用以下指令新增你想翻譯成的語言（可新增多個）：\n"
            "> `/addmylang lang:zh-TW` — 繁體中文\n"
            "> `/addmylang lang:en` — 英文\n"
            "> `/addmylang lang:ja` — 日文\n\n"
            "## Translation languages not set up\n"
            "Use the command below to add languages (you can add multiple):\n"
            "> `/addmylang lang:zh-TW` — Traditional Chinese\n"
            "> `/addmylang lang:en` — English\n"
            "> `/addmylang lang:ja` — Japanese",
            ephemeral=True,
        )
        return

    # Determine source language from channel config
    all_gc = channel_configs.get(interaction.guild_id, {})
    ch_id = message.channel.id
    if isinstance(message.channel, discord.Thread) and message.channel.parent_id:
        ch_id = message.channel.parent_id
    source_lang = all_gc[ch_id]["lang"] if ch_id in all_gc else "auto"

    target_langs = [normalize_lang(l) for l in user_langs]

    guild_glossary = get_guild_glossary(interaction.guild_id, _glossary_data)
    results = await asyncio.gather(*[
        asyncio.to_thread(translate_text, text, source_lang, lang, guild_glossary)
        for lang in target_langs
    ])

    src_label = source_lang if source_lang != "auto" else "自動偵測"
    valid_results = [(lang, t) for lang, t in zip(target_langs, results) if t]
    # Discord embed total limit is 6000 chars; divide evenly across all fields
    num_fields = 1 + len(valid_results)
    per_field = min(1024, max(100, 5500 // num_fields))

    embed = discord.Embed(title="翻譯結果", color=discord.Color.blue())
    embed.add_field(name=f"原文（{src_label}）", value=text[:per_field], inline=False)
    for lang, translated in valid_results:
        embed.add_field(name=lang, value=translated[:per_field], inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# Slash command error handlers
@slash_addlang.error
@slash_removelang.error
@slash_addterm.error
@slash_removeterm.error
@slash_addproper.error
@slash_removeproper.error
@slash_addsub.error
@slash_removesub.error
async def _perm_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("需要「管理頻道」權限。", ephemeral=True)


# ---------------------------------------------------------------------------
# Core command logic (shared by prefix and slash)
# ---------------------------------------------------------------------------

async def _do_setlang(guild_id: int, target: discord.TextChannel, lang_code: str, respond, group: str = "default") -> None:
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
        "group": group,
    }
    save_channel_config(channel_configs)
    await respond(f"Set {target.mention} as the `{normalized}` language channel (group: `{group}`).")


async def _do_unsetlang(guild_id: int, target: discord.TextChannel, respond) -> None:
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
        await respond(f"Removed {target.mention} from language channels.")
    else:
        await respond(f"{target.mention} was not a registered language channel.")


async def _do_listlang(guild_id: int, respond) -> None:
    guild_channels = channel_configs.get(guild_id, {})
    if not guild_channels:
        await respond("No language channels registered. Use `/addlang` to add one.")
        return
    groups: dict[str, list[str]] = {}
    for ch_id, info in guild_channels.items():
        ch = bot.get_channel(ch_id)
        ch_mention = ch.mention if ch else f"(unknown {ch_id})"
        g = info.get("group", "default")
        groups.setdefault(g, []).append(f"{ch_mention} → `{info['lang']}`")
    embed = discord.Embed(title="Language Channels", color=discord.Color.green())
    for g_name, lines in groups.items():
        embed.add_field(name=f"群組：{g_name}", value="\n".join(lines), inline=False)
    await respond(embed=embed)


if __name__ == "__main__":
    bot.run(TOKEN)
