"""
automod.py — Banned-image auto-moderation
==========================================

The owner bans a specific image by replying to the message that sent it
with -banimage <duration>. From then on, Jarvis checks every message's
image attachments against the banned list; a match gets the message
deleted and its author timed out for the duration set when that image
was banned.

Matching is EXACT content-hash only (SHA-256 of the raw file bytes) — a
resave, recompression, resize, or crop of a banned image will NOT match.
That trade-off was chosen deliberately to avoid extra dependencies and
false positives; see db.py's banned_images table.

Wired up from main.py via setup(bot, owner_id), and check_message(bot,
message) is called from on_message before other message processing.
"""
import datetime
import hashlib
import re

import discord
from discord.ext import commands

import db
import emoji

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

# Matches chunks like "10m", "1h", "30s", "2d", "1w" — used so a duration
# like "1h30m" also parses correctly (two chunks, summed).
_DURATION_CHUNK_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# Discord's own hard cap on how long a timeout can last.
MAX_TIMEOUT_SECONDS = 28 * 86400

# In-memory cache of config (currently just the automod log channel),
# loaded from the shared `config` table at startup — same pattern as
# vanity.py's config dict.
DEFAULT_CONFIG = {"automod_log_channel_id": "0"}
config = dict(DEFAULT_CONFIG)


def cfg_int(key: str) -> int:
    try:
        return int(config.get(key, 0))
    except (TypeError, ValueError):
        return 0


async def load_config_from_db():
    stored = await db.get_all_config()
    for key in DEFAULT_CONFIG:
        if key in stored:
            config[key] = stored[key]


# ---------------------------------------------------------------------------
# Duration parsing/formatting
# ---------------------------------------------------------------------------

def parse_duration(text: str) -> int | None:
    """Parse strings like '10m', '1h30m', '2d', '45s' into seconds.
    Returns None if nothing recognizable was found, or 0 if it parsed to
    a non-positive duration."""
    if not text:
        return None
    chunks = _DURATION_CHUNK_RE.findall(text.strip())
    if not chunks:
        return None
    total = sum(int(amount) * _UNIT_SECONDS[unit.lower()] for amount, unit in chunks)
    return total if total > 0 else None


def format_duration(seconds) -> str:
    seconds = int(seconds)
    parts = []
    for unit, unit_seconds in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= unit_seconds:
            value = seconds // unit_seconds
            seconds -= value * unit_seconds
            parts.append(f"{value}{unit}")
    return " ".join(parts) if parts else "0s"


# ---------------------------------------------------------------------------
# Attachment helpers
# ---------------------------------------------------------------------------

def _is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith("image/"):
        return True
    return att.filename.lower().endswith(IMAGE_EXTENSIONS)


def _image_attachments(message: discord.Message) -> list[discord.Attachment]:
    return [a for a in message.attachments if _is_image_attachment(a)]


async def _hash_attachment(att: discord.Attachment) -> str:
    data = await att.read()
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Message scanning — called from on_message in main.py
# ---------------------------------------------------------------------------

async def check_message(bot, message: discord.Message) -> bool:
    """Checks an incoming message's image attachments against the banned
    list. If one matches: deletes the message and times out the author.
    Returns True if action was taken, so main.py can skip further
    processing (quest tracking, command parsing) on this message."""
    if message.guild is None or message.author.bot:
        return False

    images = _image_attachments(message)
    if not images:
        return False

    for att in images:
        try:
            file_hash = await _hash_attachment(att)
        except discord.HTTPException:
            continue

        banned = await db.get_banned_image(file_hash)
        if banned is None:
            continue

        await _take_action(bot, message, banned)
        return True

    return False


async def _take_action(bot, message: discord.Message, banned: dict) -> None:
    member = message.author
    timeout_seconds = min(banned["timeout_seconds"], MAX_TIMEOUT_SECONDS)

    # Don't touch anyone with moderation permissions — most likely
    # testing or re-posting for moderation reasons, and the bot may not
    # have a high enough role to time them out anyway.
    perms = message.channel.permissions_for(member)
    if perms.administrator or perms.moderate_members:
        return

    delete_ok = True
    try:
        await message.delete()
    except discord.HTTPException:
        delete_ok = False

    timeout_ok = True
    try:
        until = discord.utils.utcnow() + datetime.timedelta(seconds=timeout_seconds)
        await member.timeout(until, reason="Sent a banned image (image automod)")
    except discord.HTTPException:
        timeout_ok = False

    notice = (
        f"{emoji.TIMEOUT} {member.mention}'s image was removed (banned image) "
        f"and they've been timed out for **{format_duration(timeout_seconds)}**."
    )
    if not delete_ok:
        notice += f"\n{emoji.WARNING} Couldn't delete the original message — check my permissions."
    if not timeout_ok:
        notice += f"\n{emoji.WARNING} Couldn't apply the timeout — check my role position/permissions."

    try:
        warn_msg = await message.channel.send(notice)
        await warn_msg.delete(delay=15)
    except discord.HTTPException:
        pass

    await _send_log_embed(bot, member, message, banned, timeout_seconds, delete_ok, timeout_ok)


async def _send_log_embed(bot, member, message, banned, timeout_seconds, delete_ok, timeout_ok):
    channel_id = cfg_int("automod_log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    embed = discord.Embed(
        title=f"{emoji.BANNED_IMAGE} Banned Image Caught",
        color=discord.Color.red(),
    )
    embed.add_field(name=f"{emoji.USER} User", value=f"{member.mention} (`{member}`)", inline=False)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    embed.add_field(name=f"{emoji.TIMEOUT} Timeout", value=format_duration(timeout_seconds), inline=True)
    embed.add_field(name="Image Hash", value=f"`{banned['hash'][:16]}...`", inline=True)
    if not delete_ok:
        embed.add_field(name=f"{emoji.WARNING} Delete failed", value="Missing permissions", inline=False)
    if not timeout_ok:
        embed.add_field(name=f"{emoji.WARNING} Timeout failed", value="Missing permissions/role position", inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"User ID: {member.id}")
    embed.timestamp = discord.utils.utcnow()

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def setup(bot, owner_id: int):
    def is_owner():
        async def predicate(ctx):
            return ctx.author.id == owner_id
        return commands.check(predicate)

    async def _get_replied_message(ctx) -> discord.Message | None:
        ref = ctx.message.reference
        if ref is None:
            return None
        if ref.resolved and isinstance(ref.resolved, discord.Message):
            return ref.resolved
        try:
            return await ctx.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None

    @bot.command(name="banimage")
    @is_owner()
    async def banimage(ctx, *, duration: str = None):
        """-banimage <duration> — owner-only. Reply to a message containing
        an image with this command to ban that exact image. Any future
        message containing it gets deleted and its sender timed out for
        <duration> (e.g. 10m, 1h, 2d, 1h30m). Max 28 days (Discord's cap)."""
        replied = await _get_replied_message(ctx)
        if replied is None:
            await ctx.send(
                f"{emoji.WARNING} Reply to the message containing the image you want to ban, "
                f"e.g. `-banimage 10m` as a reply."
            )
            return

        images = _image_attachments(replied)
        if not images:
            await ctx.send(f"{emoji.WARNING} That message doesn't have an image attachment.")
            return

        seconds = parse_duration(duration)
        if seconds is None:
            await ctx.send(
                f"{emoji.WARNING} Couldn't understand that duration. Try something like "
                f"`10m`, `1h`, `2d`, or `1h30m`."
            )
            return
        seconds = min(seconds, MAX_TIMEOUT_SECONDS)

        att = images[0]
        try:
            file_hash = await _hash_attachment(att)
        except discord.HTTPException:
            await ctx.send(f"{emoji.ERROR} Couldn't download that image to hash it — try again.")
            return

        await db.add_banned_image(file_hash, seconds, ctx.author.id, filename=att.filename)
        await ctx.send(
            f"{emoji.SUCCESS} Banned that image. Anyone who sends it now gets timed out for "
            f"**{format_duration(seconds)}** and the message deleted.\n"
            f"Hash: `{file_hash[:16]}...`"
        )

    @bot.command(name="unbanimage")
    @is_owner()
    async def unbanimage(ctx, *, hash_prefix: str = None):
        """-unbanimage — owner-only. Reply to the original banned image's
        message to un-ban it, OR run `-unbanimage <hash prefix>` using a
        prefix shown in -listbannedimages."""
        if hash_prefix:
            removed = await db.remove_banned_image_by_prefix(hash_prefix.strip())
            if removed is None:
                await ctx.send(
                    f"{emoji.WARNING} No single banned image matched that prefix — "
                    f"check `-listbannedimages` for the exact prefix."
                )
                return
            await ctx.send(f"{emoji.DELETE} Un-banned image `{removed[:16]}...`.")
            return

        replied = await _get_replied_message(ctx)
        if replied is None:
            await ctx.send(
                f"{emoji.WARNING} Reply to the original banned image's message, or run "
                f"`-unbanimage <hash prefix>` (see `-listbannedimages`)."
            )
            return

        images = _image_attachments(replied)
        if not images:
            await ctx.send(f"{emoji.WARNING} That message doesn't have an image attachment.")
            return

        try:
            file_hash = await _hash_attachment(images[0])
        except discord.HTTPException:
            await ctx.send(f"{emoji.ERROR} Couldn't download that image to hash it — try again.")
            return

        removed = await db.remove_banned_image(file_hash)
        if removed:
            await ctx.send(f"{emoji.DELETE} Un-banned that image.")
        else:
            await ctx.send(f"{emoji.WARNING} That image isn't currently banned.")

    @bot.command(name="listbannedimages", aliases=["bannedimages"])
    @is_owner()
    async def listbannedimages(ctx):
        """-listbannedimages — owner-only. Lists every currently-banned
        image, its timeout duration, and who banned it."""
        banned = await db.get_all_banned_images()
        if not banned:
            await ctx.send(f"{emoji.BANNED_IMAGE} No images are currently banned.")
            return

        embed = discord.Embed(
            title=f"{emoji.BANNED_IMAGE} Banned Images",
            description=f"{len(banned)} image(s) currently banned.",
            color=discord.Color.red(),
        )
        for entry in banned[:25]:  # embed field cap
            adder = f"<@{entry['added_by']}>"
            name = entry["filename"] or "(unnamed)"
            embed.add_field(
                name=f"`{entry['hash'][:16]}...`",
                value=(
                    f"File: {name}\n"
                    f"Timeout: {format_duration(entry['timeout_seconds'])}\n"
                    f"Banned by: {adder}"
                ),
                inline=True,
            )
        if len(banned) > 25:
            embed.set_footer(text=f"+{len(banned) - 25} more not shown.")
        await ctx.send(embed=embed)

    @bot.command(name="setautomodlogchannel")
    @is_owner()
    async def setautomodlogchannel(ctx, channel: discord.TextChannel):
        """-setautomodlogchannel #channel — owner-only. Sets where banned
        image catches get logged."""
        config["automod_log_channel_id"] = str(channel.id)
        await db.set_config("automod_log_channel_id", channel.id)
        await ctx.send(f"{emoji.SUCCESS} Automod log channel set to {channel.mention}")

    return banimage, unbanimage, listbannedimages, setautomodlogchannel
