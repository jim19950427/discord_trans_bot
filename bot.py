import os
import io
import time
import json
import threading
import asyncio
from dataclasses import dataclass
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from translator import (
    TranslationOutcome,
    translate_text,
    translate_text_nocache,
    translate_text_with_status,
    translate_many_with_status,
    get_translation_status,
    normalize_lang,
    has_translatable_content,
    log_event,
)
from config import load_channel_config, save_channel_config
from glossary import (
    load_glossary, save_glossary, get_guild_glossary,
    load_substitutions, save_substitutions, get_guild_substitutions,
    load_user_langs, save_user_langs,
    save_clusters, load_clusters,
    save_thread_clusters, load_thread_clusters,
    save_channel_pins, load_channel_pins,
)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# ── Hot-reload file watcher ────────────────────────────────────────────────
# Polls the mtime of all source files every 10 s; calls os._exit(0) on any
# change so Docker's restart policy brings the container back on new code.
# Must use os._exit (not sys.exit) — sys.exit only unwinds the calling thread
# and leaves the main process alive, silently preventing the reload.
def _watch_source_files():
    _root = os.path.dirname(os.path.abspath(__file__))
    _watched = [
        os.path.join(_root, f)
        for f in ("bot.py", "translator.py", "translation_providers.py", "config.py", "glossary.py")
    ]
    _mtimes = {f: os.path.getmtime(f) for f in _watched if os.path.exists(f)}
    while True:
        time.sleep(10)
        for f in _watched:
            try:
                if os.path.getmtime(f) != _mtimes.get(f):
                    log_event(f"[hot-reload] {os.path.basename(f)} changed — restarting")
                    os._exit(0)
            except OSError:
                pass

threading.Thread(target=_watch_source_files, daemon=True).start()
# ──────────────────────────────────────────────────────────────────────────

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
AUTO_SOURCE_LANGUAGE = "auto"


@dataclass(frozen=True)
class _ForwardResult:
    message_id: int
    text: str
    translation_succeeded: bool

    def __iter__(self):
        yield self.message_id
        yield self.text

# msg_id -> cluster dict shared by all messages in a translation group
# cluster keys:
#   channels       {channel_id: msg_id}
#   contents       {channel_id: translated_text}
#   author         display name of the original sender
#   avatar_url     avatar URL (needed for delete+resend on attachment edit)
#   source_ch      channel_id of the original message
#   source_lang    translation source mode (always "auto" for new clusters)
#   prefixes       {channel_id: blockquote_prefix_string}  (reply messages only)
#   att_names      {channel_id: [filename, ...]}  for detecting attachment changes
#   embed_count    number of embeds seen so far (for link-preview forwarding)
_msg_clusters: dict[int, dict] = {}
_MAX_CLUSTER_ENTRIES = int(os.getenv("MAX_CLUSTER_ENTRIES", "2000"))

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


def _ref_msg_link(ref_cluster: dict, channel_id: int, guild_id: int) -> str | None:
    """Return a Discord jump URL for the given channel's copy of a ref_cluster message."""
    msg_id = ref_cluster["channels"].get(channel_id)
    if not msg_id:
        return None
    thread_id = ref_cluster.get("thread_channels", {}).get(channel_id)
    link_ch = thread_id or channel_id
    return f"https://discord.com/channels/{guild_id}/{link_ch}/{msg_id}"


def _quoted_text(ref_cluster: dict, channel_id: int) -> str:
    """Return the best available quoted text for a reply, falling back to attachment links."""
    text = ref_cluster["contents"].get(channel_id, "")
    if text:
        return text
    names = ref_cluster.get("att_names", {}).get(channel_id, [])
    if not names:
        return ""
    source_ch = ref_cluster["source_ch"]
    urls = (ref_cluster.get("att_urls", {}).get(channel_id)
            or ref_cluster.get("att_urls", {}).get(source_ch, []))
    parts = []
    for i, name in enumerate(names):
        url = urls[i] if i < len(urls) else None
        parts.append(f"[{name}]({url})" if url else name)
    return "📎 " + ", ".join(parts)


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
# Cluster persistence
# ---------------------------------------------------------------------------

@tasks.loop(seconds=60)
async def _persist_clusters():
    await asyncio.to_thread(save_clusters, dict(_msg_clusters))
    await asyncio.to_thread(save_thread_clusters, dict(_thread_clusters))
    await asyncio.to_thread(save_channel_pins, dict(_channel_pins))


@bot.event
async def on_close():
    save_clusters(_msg_clusters)
    save_thread_clusters(_thread_clusters)
    save_channel_pins(_channel_pins)


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

    _msg_clusters.update(load_clusters())
    _thread_clusters.update(load_thread_clusters())
    saved_pins = load_channel_pins()
    for ch_id, pin_set in saved_pins.items():
        _channel_pins.setdefault(ch_id, set()).update(pin_set)

    if not _persist_clusters.is_running():
        _persist_clusters.start()

    log_event(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log_event(f"Loaded channel configs for {len(channel_configs)} guild(s)")
    log_event(f"Restored {len(_msg_clusters)} msg clusters, {len(_thread_clusters)} thread clusters")
    try:
        synced = await bot.tree.sync()
        log_event(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        log_event(f"Failed to sync slash commands: {e}")

    # Write a startup marker so deploy verification can confirm restart via SSH.
    _status_file = os.environ.get("STATUS_FILE", "/data/status.json")
    try:
        os.makedirs(os.path.dirname(_status_file) or ".", exist_ok=True)
        with open(_status_file, "w") as _f:
            json.dump({"last_start": time.strftime("%Y-%m-%d %H:%M:%S")}, _f)
    except Exception as _e:
        print(f"[status write failed] {_e}")


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

    # A channel's configured language describes its output, not what users
    # are allowed to type there. Providers always detect message language.
    source_lang = AUTO_SOURCE_LANGUAGE
    username = message.author.display_name
    avatar_url = str(message.author.display_avatar.url)
    guild_glossary = get_guild_glossary(message.guild.id, _glossary_data)
    guild_substitutions = get_guild_substitutions(message.guild.id, _substitutions_data)

    ref_cluster = None
    if message.reference and message.reference.message_id:
        ref_cluster = _msg_clusters.get(message.reference.message_id)

    deliveries = []
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
        target_lang = normalize_lang(info["lang"])
        quoted = _quoted_text(ref_cluster, channel_id) if ref_cluster else None
        quoted_author = ref_cluster.get("author") if ref_cluster else None
        msg_link = _ref_msg_link(ref_cluster, channel_id, message.guild.id) if ref_cluster else None
        deliveries.append((
            channel_id, target_lang, webhook_url, quoted, quoted_author,
            target_thread_id, msg_link,
        ))

    if not deliveries:
        return

    if raw_forward:
        tasks = [
            _raw_forward_send(
                content, webhook_url, username, avatar_url, attachments, stickers,
                quoted, quoted_author, target_thread_id, msg_link,
            )
            for _, _, webhook_url, quoted, quoted_author, target_thread_id, msg_link in deliveries
        ]
    else:
        # Attachments and stickers are forwarded without asking a provider to
        # translate an empty body.  Otherwise translate once before fan-out;
        # outcomes are looked up by canonical target key below.
        outcomes = (
            await asyncio.to_thread(
                translate_many_with_status,
                content,
                [target_lang for _, target_lang, *_ in deliveries],
                guild_glossary,
                guild_substitutions,
            )
            if content else {}
        )
        tasks = [
            _send_pretranslated(
                outcomes.get(target_lang, TranslationOutcome(None, False)),
                target_lang, webhook_url, username, avatar_url,
                attachments, stickers, quoted, quoted_author,
                target_thread_id, msg_link,
            )
            for _, target_lang, webhook_url, quoted, quoted_author, target_thread_id, msg_link in deliveries
        ]

    target_channel_ids = [channel_id for channel_id, *_ in deliveries]
    target_thread_ids = [target_thread_id for *_, target_thread_id, _ in deliveries]

    # return_exceptions=True: one channel's send failing (e.g. an unhandled
    # discord.HTTPException) must not abort the cluster build below — that
    # would silently drop every OTHER channel's already-sent message from
    # tracking too, breaking edit/delete/pin sync for them as well.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for ch_id, result in zip(target_channel_ids, results):
        if isinstance(result, BaseException):
            log_event(f"[forward] task failed for channel {ch_id}: {result}")
    results = [None if isinstance(r, BaseException) else r for r in results]

    cluster: dict = {
        "channels": {source_ch_id: message.id},
        "thread_channels": ({source_ch_id: message.channel.id} if thread_map else {}),
        "contents": {source_ch_id: content},
        "author": username,
        "avatar_url": avatar_url,
        "source_ch": source_ch_id,
        "source_lang": source_lang,
        "raw_forward": raw_forward,
        "prefixes": {},
        "att_names": {source_ch_id: [a.filename for a in attachments]},
        "att_urls":  {source_ch_id: [a.url for a in attachments]},
        "embed_count": len(message.embeds),
    }
    for ch_id, tid, result in zip(target_channel_ids, target_thread_ids, results):
        if result is not None:
            sent_id, sent_text = result
            cluster["channels"][ch_id] = sent_id
            cluster["contents"][ch_id] = sent_text or ""
            cluster["att_names"][ch_id] = [a.filename for a in attachments]
            cluster["att_urls"][ch_id] = [a.url for a in attachments]
            if tid is not None:
                cluster["thread_channels"][ch_id] = tid

    if ref_cluster:
        ref_author = ref_cluster.get("author", "")
        for ch_id in target_channel_ids:
            quoted = _quoted_text(ref_cluster, ch_id)
            if quoted:
                link = _ref_msg_link(ref_cluster, ch_id, message.guild.id)
                lines = quoted.splitlines()
                pl: list[str] = []
                if ref_author and lines:
                    first = f"**{ref_author}**: {lines[0]}"
                    pl.append(f"> {first} [↗]({link})" if link else f"> {first}")
                    pl.extend(f"> {l}" for l in lines[1:])
                else:
                    pl.extend(f"> {l}" for l in lines)
                cluster["prefixes"][ch_id] = "\n".join(pl)

    _store_cluster(cluster)

    # Schedule a delayed retry only when the explicit provider status says
    # translation failed. Equal text can be a valid auto-detected success.
    # Skip messages with nothing translatable (pure mention/custom-emoji/Unicode
    # emoji) — those intentionally come back unchanged, that's not a failure.
    if not raw_forward and content and has_translatable_content(content):
        for ch_id, result in zip(target_channel_ids, results):
            if result is None:
                continue
            sent_id, sent_text = result
            translation_succeeded = getattr(
                result,
                "translation_succeeded",
                sent_text.strip() != content.strip(),
            )
            if not translation_succeeded:
                asyncio.create_task(
                    _retry_translate(
                        content, source_lang,
                        normalize_lang(guild_channels[ch_id]["lang"]),
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
    raw_forward = new_content.startswith(RAW_FORWARD_PREFIX)
    if raw_forward:
        new_content = new_content[len(RAW_FORWARD_PREFIX):].lstrip()
    current_attachments = list(message.attachments)
    current_stickers = [s for s in message.stickers if s.format != discord.StickerFormatType.lottie]
    current_embeds = message.embeds

    guild_glossary = get_guild_glossary(channel.guild.id, _glossary_data)
    guild_substitutions = get_guild_substitutions(channel.guild.id, _substitutions_data)

    edit_targets = [
        (
            ch_id, msg_id, normalize_lang(guild_channels[ch_id]["lang"]),
            guild_channels[ch_id]["webhook_url"],
        )
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
                log_event(f"Failed to forward embeds to channel {ch_id}: {e}")

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

        outcomes = (
            await asyncio.to_thread(
                translate_many_with_status,
                new_content,
                [lang for _, _, lang, _ in edit_targets],
                guild_glossary,
                guild_substitutions,
            )
            if new_content and not raw_forward else {}
        )
        send_results = await asyncio.gather(*[
            (
                _raw_forward_send(
                    new_content, wh_url, cluster["author"], cluster["avatar_url"],
                    current_attachments, current_stickers,
                    cluster.get("prefixes", {}).get(ch_id), None,
                )
                if raw_forward else
                _send_pretranslated(
                    outcomes.get(lang, TranslationOutcome(None, False)), lang,
                    wh_url, cluster["author"], cluster["avatar_url"],
                    current_attachments, current_stickers,
                    cluster.get("prefixes", {}).get(ch_id), None,
                )
            )
            for ch_id, _, lang, wh_url in edit_targets
        ])

        cluster["contents"][source_ch_id] = new_content
        cluster["raw_forward"] = raw_forward
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
        outcomes = (
            await asyncio.to_thread(
                translate_many_with_status,
                new_content,
                [lang for _, _, lang, _ in edit_targets],
                guild_glossary,
                guild_substitutions,
            )
            if not raw_forward else {}
        )
        edit_results = await asyncio.gather(*[
            _edit_pretranslated(
                (
                    TranslationOutcome(new_content, True)
                    if raw_forward else outcomes.get(lang, TranslationOutcome(None, False))
                ),
                wh_url, msg_id, ch_id, cluster,
            )
            for ch_id, msg_id, lang, wh_url in edit_targets
        ])
        cluster["contents"][source_ch_id] = new_content
        cluster["raw_forward"] = raw_forward
        for (ch_id, _, _, _), translated in zip(edit_targets, edit_results):
            if translated:
                cluster["contents"][ch_id] = translated


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    cluster = _msg_clusters.pop(payload.message_id, None)
    if not cluster:
        return

    # Clear every sibling key for this cluster BEFORE awaiting the deletes
    # below. The bot's own deletion of each mirror message also fires this
    # same event, and without clearing synchronously first, those cascading
    # events can each still find the cluster (since the delete gather below
    # yields control) and redundantly re-run this whole handler for the same
    # cluster — observed in production as the same message getting repeated
    # "already deleted" 404s.
    for msg_id in list(cluster["channels"].values()):
        _msg_clusters.pop(msg_id, None)

    guild_channels = _guild_channels_for(payload.channel_id)

    await asyncio.gather(*[
        _delete_webhook_message(guild_channels[ch_id]["webhook_url"], msg_id, ch_id)
        for ch_id, msg_id in cluster["channels"].items()
        if msg_id != payload.message_id
        and ch_id in guild_channels
        and guild_channels[ch_id].get("webhook_url")
    ])


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
            log_event(f"Failed to add reaction in channel {channel_id}: {e}")


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
            log_event(f"Failed to remove reaction in channel {channel_id}: {e}")


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
            log_event(f"Failed to clear reactions in channel {channel_id}: {e}")


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
            log_event(f"Failed to clear emoji reaction in channel {channel_id}: {e}")


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
                log_event(f"Failed to pin {cluster_msg_id} in channel {ch_id}: {e}")

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
                log_event(f"Failed to unpin {cluster_msg_id} in channel {ch_id}: {e}")


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
    thread_map: dict[int, int] = {thread.parent_id: thread.id}

    thread_targets = [
        (ch_id, normalize_lang(info["lang"]), target_ch)
        for ch_id, info in gc.items()
        if ch_id != thread.parent_id
        and isinstance((target_ch := bot.get_channel(ch_id)), discord.TextChannel)
    ]
    outcomes = (
        await asyncio.to_thread(
            translate_many_with_status,
            thread.name,
            [target_lang for _, target_lang, _ in thread_targets],
        )
        if thread_targets else {}
    )

    for ch_id, target_lang, target_ch in thread_targets:
        translated_name = outcomes.get(
            target_lang, TranslationOutcome(None, False)
        ).text or thread.name
        try:
            new_thread = await target_ch.create_thread(
                name=translated_name[:100],
                type=discord.ChannelType.public_thread,
            )
            thread_map[ch_id] = new_thread.id
        except Exception as e:
            log_event(f"[thread] failed to create in ch={ch_id}: {e}")

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
    delay: int = 60,
) -> None:
    await asyncio.sleep(delay)
    outcome = await asyncio.to_thread(
        translate_text_with_status, text, src, dest, glossary or {}
    )
    translated = outcome.text
    if not outcome.provider_succeeded or not translated:
        log_event(f"[retry] still failed ({src}->{dest}): {repr(text)}")
        return
    prefix = cluster.get("prefixes", {}).get(ch_id, "")
    full_content = f"{prefix}\n{translated}" if prefix else translated
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            await webhook.edit_message(msg_id, content=full_content)
        cluster["contents"][ch_id] = translated
        log_event(f"[retry] updated ({src}->{dest}): {repr(translated)}")
    except Exception as e:
        log_event(f"[retry] edit failed msg={msg_id} ch={ch_id}: {e}")


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
    msg_link: str | None = None,
) -> _ForwardResult | None:
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
            log_event(f"Download failed ({filename}): {e}")

    if not text and not files:
        return None

    parts: list[str] = []
    if quoted_content:
        lines = quoted_content.splitlines()
        if quoted_author and lines:
            first = f"**{quoted_author}**: {lines[0]}"
            parts.append(f"> {first} [↗]({msg_link})" if msg_link else f"> {first}")
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

    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            msg = await webhook.send(**send_kwargs)
            return msg.id, text or ""
    except Exception as e:
        # Never log webhook_url — it embeds the webhook's auth token.
        log_event(f"[forward] send failed (author={username!r}, files={len(files)}): {e}")
        return None


async def _send_pretranslated(
    outcome: TranslationOutcome,
    dest: str,
    webhook_url: str,
    username: str,
    avatar_url: str,
    attachments: list,
    stickers: list,
    quoted_content: str | None,
    quoted_author: str | None = None,
    thread_id: int | None = None,
    msg_link: str | None = None,
) -> _ForwardResult | None:
    translated = outcome.text

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
            log_event(f"Download failed ({filename}): {e}")

    if not translated and not files:
        return None

    parts: list[str] = []
    if quoted_content:
        lines = quoted_content.splitlines()
        if quoted_author and lines:
            first = f"**{quoted_author}**: {lines[0]}"
            parts.append(f"> {first} [↗]({msg_link})" if msg_link else f"> {first}")
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

    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            msg = await webhook.send(**send_kwargs)
            return _ForwardResult(
                msg.id,
                translated or "",
                outcome.provider_succeeded,
            )
    except Exception as e:
        # Never log webhook_url — it embeds the webhook's auth token.
        log_event(f"[forward] send failed (author={username!r}, dest={dest}, files={len(files)}): {e}")
        return None


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
    msg_link: str | None = None,
) -> _ForwardResult | None:
    """Single-target compatibility wrapper around pretranslated delivery."""
    outcome = TranslationOutcome(None, True)
    if text:
        outcome = await asyncio.to_thread(
            translate_text_with_status,
            text,
            src,
            dest,
            glossary or {},
            substitutions or {},
        )
    return await _send_pretranslated(
        outcome, dest, webhook_url, username, avatar_url,
        attachments, stickers, quoted_content, quoted_author,
        thread_id, msg_link,
    )


async def _edit_pretranslated(
    outcome: TranslationOutcome,
    webhook_url: str,
    msg_id: int,
    ch_id: int,
    cluster: dict,
) -> str | None:
    translated = outcome.text
    if not translated:
        return None

    prefix = cluster.get("prefixes", {}).get(ch_id, "")
    full_content = f"{prefix}\n{translated}" if prefix else translated

    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            await webhook.edit_message(msg_id, content=full_content)
    except Exception as e:
        log_event(f"Failed to edit webhook message {msg_id} in channel {ch_id}: {e}")
        return None

    return translated


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
    """Single-target compatibility wrapper around pretranslated editing."""
    outcome = await asyncio.to_thread(
        translate_text_with_status,
        text, src, dest, glossary or {}, substitutions or {},
    )
    return await _edit_pretranslated(outcome, webhook_url, msg_id, ch_id, cluster)


async def _delete_webhook_message(webhook_url: str, msg_id: int, ch_id: int) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session)
            await webhook.delete_message(msg_id)
    except Exception as e:
        log_event(f"Failed to delete webhook message {msg_id} in channel {ch_id}: {e}")


class _ClusterAttachment:
    """Stand-in for discord.Attachment, built from the url/filename pairs a
    cluster already persists. Only .url/.filename are read by the download
    helpers in _send_pretranslated / _raw_forward_send, so this is enough to
    re-run a forward without the original discord.Message object."""
    __slots__ = ("url", "filename")

    def __init__(self, url: str, filename: str):
        self.url = url
        self.filename = filename


async def _retry_missing_channels(cluster: dict, guild_id: int) -> tuple[list[int], list[int]]:
    """Forward the source message to any group channels missing from the
    cluster — i.e. channels whose original webhook.send() failed and were
    silently dropped (see _translate_and_send / _raw_forward_send). Returns
    (succeeded_channel_ids, failed_channel_ids).

    Text is translated as one batch before retry fan-out. Raw-forward and
    attachment-only clusters are delivered without invoking translation.
    """
    source_ch_id = cluster["source_ch"]
    group_channels = _guild_channels_for(source_ch_id)
    missing = [
        ch_id for ch_id, info in group_channels.items()
        if ch_id != source_ch_id
        and info.get("webhook_url")
        and ch_id not in cluster["channels"]
    ]
    if not missing:
        return [], []

    source_text = cluster["contents"].get(source_ch_id, "")
    username = cluster.get("author", "")
    avatar_url = cluster.get("avatar_url", "")
    attachments = [
        _ClusterAttachment(url, name)
        for url, name in zip(
            cluster.get("att_urls", {}).get(source_ch_id, []),
            cluster.get("att_names", {}).get(source_ch_id, []),
        )
    ]
    guild_glossary = get_guild_glossary(guild_id, _glossary_data)
    guild_substitutions = get_guild_substitutions(guild_id, _substitutions_data)
    outcomes = (
        await asyncio.to_thread(
            translate_many_with_status,
            source_text,
            [normalize_lang(group_channels[ch_id]["lang"]) for ch_id in missing],
            guild_glossary,
            guild_substitutions,
        )
            if source_text and not cluster.get("raw_forward") else {}
    )

    # Thread routing mirrors on_message: the source's own thread id (if any)
    # looks up the matching per-channel thread ids via the same registry.
    source_thread_id = cluster.get("thread_channels", {}).get(source_ch_id)
    thread_map = _thread_clusters.get(source_thread_id) if source_thread_id else None

    async def _retry_one(ch_id: int):
        info = group_channels[ch_id]
        target_thread_id = thread_map.get(ch_id) if thread_map else None
        if thread_map is not None and target_thread_id is None:
            return ch_id, None
        # quoted_content/quoted_author are deliberately not passed here:
        # cluster["prefixes"][ch_id] already holds a fully "> "-formatted
        # reply-quote block, but _send_pretranslated expects the *raw* quote
        # text and applies its own "> " prefixing — passing the pre-formatted
        # block through would double-quote it (e.g. "> > **name**: ..."). The
        # tradeoff: a backfilled message loses its reply-quote header if the
        # original had one; the alternative (visibly broken quoting) is worse.
        target_lang = normalize_lang(info["lang"])
        result = (
            await _raw_forward_send(
                source_text, info["webhook_url"], username, avatar_url,
                attachments, [], None, None, target_thread_id, None,
            )
            if cluster.get("raw_forward") else
            await _send_pretranslated(
                outcomes.get(target_lang, TranslationOutcome(None, False)), target_lang,
                info["webhook_url"], username, avatar_url,
                attachments, [],  # stickers aren't persisted on the cluster
                None, None, target_thread_id, None,
            )
        )
        return ch_id, result

    pairs = await asyncio.gather(*[_retry_one(ch_id) for ch_id in missing])

    succeeded, failed = [], []
    for ch_id, result in pairs:
        if result is None:
            failed.append(ch_id)
            continue
        sent_id, sent_text = result
        cluster["channels"][ch_id] = sent_id
        cluster["contents"][ch_id] = sent_text or ""
        cluster["att_names"][ch_id] = cluster.get("att_names", {}).get(source_ch_id, [])
        cluster["att_urls"][ch_id] = cluster.get("att_urls", {}).get(source_ch_id, [])
        if thread_map is not None:
            tid = thread_map.get(ch_id)
            if tid is not None:
                cluster.setdefault("thread_channels", {})[ch_id] = tid
        succeeded.append(ch_id)

    if succeeded:
        _store_cluster(cluster)
        log_event(f"[forward-retry] backfilled channels {succeeded} for source msg in ch={source_ch_id}")
    if failed:
        log_event(f"[forward-retry] still failed for channels {failed} for source msg in ch={source_ch_id}")

    return succeeded, failed


async def _do_retranslate(parent_ch_id: int, msg_id: int, cluster: dict, guild_id: int) -> str | None:
    """Re-translate a specific message and edit it in place. Returns new text or None."""
    guild_channels = _guild_channels_for(parent_ch_id)
    if parent_ch_id not in guild_channels:
        return None
    info = guild_channels[parent_ch_id]
    source_ch = cluster["source_ch"]
    source_text = cluster["contents"].get(source_ch, "")
    source_lang = AUTO_SOURCE_LANGUAGE
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
        log_event(f"[retranslate] ({source_lang}->{target_lang}) updated msg={msg_id}")
        return translated
    except Exception as e:
        log_event(f"[retranslate] edit failed msg={msg_id}: {e}")
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

    # Whatever copy the user reacted on, also retry any channels the cluster
    # never reached in the first place — the cluster is shared, so this
    # backfill applies regardless of which channel triggered the reaction.
    await _retry_missing_channels(cluster, guild_id)

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


async def _ui_msg(user_id: int, text_zh: str) -> str:
    """Return text_zh translated to the user's first registered language (LRU-cached)."""
    langs = _user_langs_data.get(str(user_id), [])
    if not langs:
        return text_zh
    target = normalize_lang(langs[0])
    if target.lower().startswith("zh"):
        return text_zh
    translated = await asyncio.to_thread(translate_text, text_zh, "zh-TW", target)
    return translated or text_zh


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

_STATUS_UNAVAILABLE = "尚無資料"
_SAFE_STATUS_REASONS = frozenset({
    "request_error", "invalid_json", "invalid_response", "circuit_open",
    "azure_failed", "libretranslate_failed", "missing_key", "empty_response",
    "partial_response",
})
_SAFE_CIRCUIT_STATES = frozenset({"closed", "open", "half_open"})


def _status_timestamp(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"<t:{int(value)}:R>"
    return _STATUS_UNAVAILABLE


def _status_number(value, suffix: str = "") -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{int(value)}{suffix}"
    return _STATUS_UNAVAILABLE


def _status_reason(value) -> str:
    if isinstance(value, str):
        if value in _SAFE_STATUS_REASONS:
            return value
        if (
            value.startswith("http_")
            and value[5:].isdigit()
            and 100 <= int(value[5:]) <= 599
        ):
            return value
    return _STATUS_UNAVAILABLE


def _status_languages(value) -> str:
    if not isinstance(value, list):
        return _STATUS_UNAVAILABLE
    languages = [
        language for language in value
        if isinstance(language, str)
        and language.replace("-", "").replace("_", "").isalnum()
        and len(language) <= 20
    ]
    return "、".join(languages) if languages else _STATUS_UNAVAILABLE


def _provider_last_result(provider: dict) -> str:
    success_at = provider.get("last_success_at")
    failure_at = provider.get("last_failure_at")
    if isinstance(success_at, (int, float)) and not isinstance(success_at, bool) and (
        not isinstance(failure_at, (int, float)) or isinstance(failure_at, bool)
        or success_at >= failure_at
    ):
        return f"成功（{_status_timestamp(success_at)}）"
    if isinstance(failure_at, (int, float)) and not isinstance(failure_at, bool):
        return (
            f"失敗（{_status_timestamp(failure_at)}；"
            f"{_status_reason(provider.get('last_failure_reason'))}）"
        )
    return _STATUS_UNAVAILABLE


def _format_translation_status(status: dict) -> discord.Embed:
    """Render only the health data deliberately approved for the status command."""
    if not isinstance(status, dict):
        status = {}
    azure = status.get("azure") if isinstance(status.get("azure"), dict) else {}
    libretranslate = (
        status.get("libretranslate")
        if isinstance(status.get("libretranslate"), dict)
        else {}
    )
    fallback = status.get("fallback") if isinstance(status.get("fallback"), dict) else {}
    probe = (
        status.get("libretranslate_probe")
        if isinstance(status.get("libretranslate_probe"), dict)
        else None
    )

    circuit_state = azure.get("circuit_state")
    circuit = (
        circuit_state
        if isinstance(circuit_state, str) and circuit_state in _SAFE_CIRCUIT_STATES
        else _STATUS_UNAVAILABLE
    )
    azure_value = "\n".join((
        f"設定：{'已設定' if azure.get('configured') is True else '未設定'}",
        f"熔斷器：{circuit}",
        f"最近結果：{_provider_last_result(azure)}",
        f"最近延遲：{_status_number(azure.get('last_latency_ms'), ' ms')}",
        f"最近目標數：{_status_number(azure.get('last_target_count'))}",
    ))

    if probe is None:
        libre_result = "未探測（被動結果）"
        libre_languages = _STATUS_UNAVAILABLE
    else:
        probe_latency = _status_number(probe.get("latency_ms"), " ms")
        if probe.get("healthy") is True:
            libre_result = f"健康（{probe_latency}）"
        else:
            libre_result = f"失敗（{probe_latency}；{_status_reason(probe.get('failure_reason'))}）"
        libre_languages = _status_languages(probe.get("languages"))
    libre_value = "\n".join((
        f"探測：{libre_result}",
        f"被動結果：{_provider_last_result(libretranslate)}",
        f"已載入語言：{libre_languages}",
    ))

    fallback_value = "\n".join((
        f"最近備援：{_status_timestamp(fallback.get('last_at'))}",
        f"原因：{_status_reason(fallback.get('reason'))}",
    ))

    embed = discord.Embed(title="翻譯服務狀態", color=discord.Color.blue())
    embed.add_field(name="Azure Translator", value=azure_value, inline=False)
    embed.add_field(name="LibreTranslate", value=libre_value, inline=False)
    embed.add_field(name="最近備援", value=fallback_value, inline=False)
    return embed


@bot.tree.command(name="translation-status", description="查看翻譯服務健康狀態")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_translation_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        status = await asyncio.to_thread(get_translation_status, probe_libre=True)
        embed = _format_translation_status(status)
    except Exception:
        embed = discord.Embed(
            title="翻譯服務狀態",
            description="暫時無法取得狀態，翻譯服務不受此查詢影響。",
            color=discord.Color.orange(),
        )
    await interaction.followup.send(embed=embed, ephemeral=True)

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
    uid = interaction.user.id
    cluster = _msg_clusters.get(message.id)
    if not cluster:
        await interaction.response.send_message(
            await _ui_msg(uid, "找不到此訊息的原文記錄。（僅追蹤容器重啟後發送的訊息）"),
            ephemeral=True,
        )
        return

    source_ch_id = cluster["source_ch"]
    source_text = cluster["contents"].get(source_ch_id, "")
    source_lang = cluster["source_lang"]
    author = cluster.get("author", "")

    source_ch = bot.get_channel(source_ch_id)
    ch_mention = source_ch.mention if source_ch else f"(#{source_ch_id})"

    title, f_channel, f_sender, f_content, no_text = await asyncio.gather(
        _ui_msg(uid, "原文"),
        _ui_msg(uid, f"來源頻道（{source_lang}）"),
        _ui_msg(uid, "發送者"),
        _ui_msg(uid, "原文內容"),
        _ui_msg(uid, "（無文字）"),
    )
    embed = discord.Embed(title=title, color=discord.Color.greyple())
    embed.add_field(name=f_channel, value=ch_mention, inline=True)
    if author:
        embed.add_field(name=f_sender, value=author, inline=True)
    embed.add_field(name=f_content, value=(source_text[:1024] or no_text), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.context_menu(name="重新翻譯")
async def retranslate_context_menu(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)

    uid = interaction.user.id
    cluster = _msg_clusters.get(message.id)
    if not cluster:
        await interaction.followup.send(
            await _ui_msg(uid, "找不到此訊息的翻譯記錄。（僅追蹤容器重啟後發送的訊息）"),
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id
    if guild_id is None:
        await interaction.followup.send(
            await _ui_msg(uid, "找不到對應的伺服器設定。"), ephemeral=True
        )
        return

    # Whatever copy this was used on, also retry any channels the cluster
    # never reached in the first place (e.g. a channel whose webhook.send()
    # failed on the original forward) — the cluster is shared across copies.
    backfilled, still_missing = await _retry_missing_channels(cluster, guild_id)

    # Find parent_ch_id for this message in the cluster
    parent_ch_id: int | None = None
    for pid, mid in cluster["channels"].items():
        if mid == message.id:
            parent_ch_id = pid
            break
    if parent_ch_id is None:
        await interaction.followup.send(
            await _ui_msg(uid, "無法找到此訊息對應的頻道。"), ephemeral=True
        )
        return

    if parent_ch_id == cluster["source_ch"]:
        if backfilled:
            names = "、".join(f"<#{c}>" for c in backfilled)
            await interaction.followup.send(f"已補發到：{names}", ephemeral=True)
        elif still_missing:
            names = "、".join(f"<#{c}>" for c in still_missing)
            await interaction.followup.send(f"補發失敗：{names}", ephemeral=True)
        else:
            await interaction.followup.send(
                await _ui_msg(uid, "此訊息是原文，無法重新翻譯。"), ephemeral=True
            )
        return

    translated = await _do_retranslate(parent_ch_id, message.id, cluster, guild_id)
    if translated:
        note = f"（另補發到：{'、'.join(f'<#{c}>' for c in backfilled)}）" if backfilled else ""
        await interaction.followup.send(await _ui_msg(uid, "已重新翻譯。") + note, ephemeral=True)
    else:
        await interaction.followup.send(
            await _ui_msg(uid, "重新翻譯失敗，或翻譯結果與原文相同。"), ephemeral=True
        )


@bot.tree.context_menu(name="翻譯此訊息")
async def translate_context_menu(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)

    uid = interaction.user.id
    text = message.content
    if not text:
        await interaction.followup.send(
            await _ui_msg(uid, "此訊息沒有文字內容。"), ephemeral=True
        )
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
    source_lang = AUTO_SOURCE_LANGUAGE

    target_langs = list(dict.fromkeys(normalize_lang(lang) for lang in user_langs))

    guild_glossary = get_guild_glossary(interaction.guild_id, _glossary_data)
    outcomes = await asyncio.to_thread(
        translate_many_with_status, text, target_langs, guild_glossary
    )

    src_label = source_lang if source_lang != "auto" else "自動偵測"
    valid_results = [
        (lang, outcomes[lang].text)
        for lang in target_langs
        if outcomes.get(lang) and outcomes[lang].text
    ]
    # Discord embed total limit is 6000 chars; divide evenly across all fields
    num_fields = 1 + len(valid_results)
    per_field = min(1024, max(100, 5500 // num_fields))

    embed = discord.Embed(title="翻譯結果", color=discord.Color.blue())
    embed.add_field(name=f"原文（{src_label}）", value=text[:per_field], inline=False)
    for lang, translated in valid_results:
        embed.add_field(name=lang, value=translated[:per_field], inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# Slash command error handlers
@slash_translation_status.error
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
