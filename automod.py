"""
automod.py — Banned-image auto-moderation
==========================================

The owner bans a specific image by replying to the message that sent it
with -banimage <duration>. From then on, Jarvis checks every message's
image attachments against the banned list; a match gets the message
deleted and its author timed out for the duration set when that image
was banned.

Matching is PERCEPTUAL (a "phash" of what the image actually looks
like, via Pillow + imagehash), not an exact byte hash. This was changed
from an earlier exact-hash version after discovering Discord's own CDN
doesn't always re-serve byte-identical copies of what looks like "the
same" re-uploaded file (especially for GIFs/larger PNGs), which made
exact hashing miss real repeat-offenses. Perceptual hashing compares a
small "fingerprint" of the image's visual content, so resizes,
recompressions, and Discord's own reprocessing don't cause a miss.

Two images are considered a match if their phash Hamming distance is
<= the configured threshold (default 5, tunable via
-setautomodthreshold). Matching requires scanning every banned image's
stored hash each time (cheap — a few microseconds per comparison — see
_find_best_match), since a distance check can't be done as a plain SQL
equality lookup the way an exact hash could.

Wired up from main.py via setup(bot, owner_id), and check_message(bot,
message) is called from on_message before other message processing.
"""
import datetime
import io
import re

import discord
import imagehash
from discord.ext import commands
from PIL import Image, UnidentifiedImageError

import db
import emoji

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

# Matches chunks like "10m", "1h", "30s", "2d", "1w" — used so a duration
# like "1h30m" also parses correctly (two chunks, summed).
_DURATION_CHUNK_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# Discord's own hard cap on how long a timeout can last.
MAX_TIMEOUT_SECONDS = 28 * 86400

# Perceptual hash size (8 -> a 64-bit/16-hex-char fingerprint). Bigger
# catches finer detail but is marginally more CPU per image — 8 is the
# imagehash library's own default and is plenty for "is this the same
# meme/image" style matching.
PHASH_SIZE = 8

# In-memory cache of config (automod log channel + match threshold),
# loaded from the shared `config` table at startup — same pattern as
# vanity.py's config dict.
DEFAULT_CONFIG = {"automod_log_channel_id": "0", "automod_phash_threshold": "5"}
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
# Attachment / perceptual-hash helpers
# ---------------------------------------------------------------------------

def _is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith("image/"):
        return True
    return att.filename.lower().endswith(IMAGE_EXTENSIONS)


def _image_attachments(message: discord.Message) -> list[discord.Attachment]:
    """Collects image attachments from the message itself AND, if it's a
    forwarded message, from the forwarded snapshot(s).

    Discord's "forward" feature doesn't copy the original message's
    attachments onto message.attachments — they only show up under
    message.message_snapshots[i].attachments (discord.py's
    MessageSnapshot). Without this, a forwarded copy of a banned image
    sailed straight past automod since message.attachments was always
    empty for it."""
    atts = list(message.attachments)
    for snapshot in getattr(message, "message_snapshots", None) or []:
        atts.extend(snapshot.attachments)
    return [a for a in atts if _is_image_attachment(a)]


async def _phash_attachment(att: discord.Attachment) -> imagehash.ImageHash | None:
    """Downloads an attachment and computes its perceptual hash. Returns
    None (and lets the caller log why) if it can't be downloaded or
    isn't a decodable image, rather than raising — a single bad
    attachment shouldn't block scanning the rest of a message."""
    try:
        data = await att.read()
    except discord.HTTPException as e:
        _log(f"couldn't download attachment {att.filename!r}: {e}")
        return None

    try:
        with Image.open(io.BytesIO(data)) as img:
            return imagehash.phash(img, hash_size=PHASH_SIZE)
    except (UnidentifiedImageError, OSError) as e:
        _log(f"couldn't decode attachment {att.filename!r} as an image: {e}")
        return None


def _hash_to_str(h: imagehash.ImageHash) -> str:
    return str(h)


def _str_to_hash(s: str) -> imagehash.ImageHash | None:
    try:
        return imagehash.hex_to_hash(s)
    except (ValueError, TypeError):
        return None


async def _find_best_match(phash: imagehash.ImageHash, threshold: int) -> tuple[dict, int] | None:
    """Scans every banned image and returns (entry, distance) for the
    closest one within `threshold`, or None if nothing's close enough.
    A full scan per check is intentional — banned-image lists for a
    single-server moderation bot are expected to stay small (tens to
    low hundreds), so this stays well under a millisecond of CPU even
    checked against every entry."""
    banned = await db.get_all_banned_images()
    best = None
    for entry in banned:
        stored = _str_to_hash(entry["hash"])
        if stored is None:
            continue
        distance = phash - stored
        if distance <= threshold and (best is None or distance < best[1]):
            best = (entry, distance)
    return best


# ---------------------------------------------------------------------------
# Message scanning — called from on_message in main.py
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"[automod] {msg}")


async def check_message(bot, message: discord.Message) -> bool:
    """Checks an incoming message's image attachments against the banned
    list (perceptual-hash match within the configured threshold). If one
    matches: deletes the message and times out the author. Returns True
    if action was taken, so main.py can skip further processing (quest
    tracking, command parsing) on this message.

    Every branch below logs to console (prefixed [automod]) so a
    "nothing happened" report can be diagnosed from the bot's console
    output instead of guessing."""
    if message.guild is None or message.author.bot:
        return False

    images = _image_attachments(message)
    if not images:
        return False

    _log(f"scanning {len(images)} image attachment(s) from {message.author} "
         f"in #{message.channel} ({message.guild.name})")

    threshold = cfg_int("automod_phash_threshold") or int(DEFAULT_CONFIG["automod_phash_threshold"])

    for att in images:
        phash = await _phash_attachment(att)
        if phash is None:
            continue

        _log(f"attachment {att.filename!r} phash: {_hash_to_str(phash)}")

        match = await _find_best_match(phash, threshold)
        if match is None:
            _log(f"no match within threshold {threshold} — not acting")
            continue

        banned, distance = match
        _log(f"MATCH — banned image {banned['hash']} (distance {distance}/{threshold}), taking action")
        await _take_action(bot, message, banned, distance)
        return True

    return False


async def _take_action(bot, message: discord.Message, banned: dict, distance: int) -> None:
    member = message.author
    timeout_seconds = min(banned["timeout_seconds"], MAX_TIMEOUT_SECONDS)

    # Don't touch anyone with moderation permissions — most likely
    # testing or re-posting for moderation reasons, and the bot may not
    # have a high enough role to time them out anyway.
    author_perms = message.channel.permissions_for(member)
    if author_perms.administrator or author_perms.moderate_members:
        _log(f"{member} has admin/moderate_members in this channel — exempted, not acting")
        return

    # Check the BOT's own permissions up front and log exactly what's
    # missing, rather than letting delete()/timeout()/send() fail
    # silently later. A channel-specific permission overwrite (e.g. no
    # View Channel or Manage Messages just in this one channel) is the
    # single most common reason this looks like it "does nothing."
    me = message.guild.me
    bot_perms = message.channel.permissions_for(me)
    missing = []
    if not bot_perms.view_channel:
        missing.append("View Channel")
    if not bot_perms.manage_messages:
        missing.append("Manage Messages")
    if not bot_perms.send_messages:
        missing.append("Send Messages")
    if not me.guild_permissions.moderate_members:
        missing.append("Timeout Members (guild-wide)")
    if missing:
        _log(f"MISSING PERMISSIONS in #{message.channel}: {', '.join(missing)}")

    delete_ok = True
    try:
        await message.delete()
        _log(f"deleted message {message.id}")
    except discord.HTTPException as e:
        delete_ok = False
        _log(f"delete FAILED: {e}")

    timeout_ok = True
    try:
        until = discord.utils.utcnow() + datetime.timedelta(seconds=timeout_seconds)
        await member.timeout(until, reason="Sent a banned image (image automod)")
        _log(f"timed out {member} for {format_duration(timeout_seconds)}")
    except discord.HTTPException as e:
        timeout_ok = False
        _log(f"timeout FAILED: {e}")

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
    except discord.HTTPException as e:
        _log(f"couldn't send the in-channel notice either: {e}")

    await _send_log_embed(bot, member, message, banned, distance, timeout_seconds, delete_ok, timeout_ok)


async def _send_log_embed(bot, member, message, banned, distance, timeout_seconds, delete_ok, timeout_ok):
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
    embed.add_field(name="Match", value=f"`{banned['hash']}` (dist {distance})", inline=True)
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
        an image with this command to ban it (and any close visual match —
        resizes/recompressions included). Any future message containing a
        match gets deleted and its sender timed out for <duration> (e.g.
        10m, 1h, 2d, 1h30m). Max 28 days (Discord's cap)."""
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
        phash = await _phash_attachment(att)
        if phash is None:
            await ctx.send(f"{emoji.ERROR} Couldn't read that as an image — try again.")
            return

        threshold = cfg_int("automod_phash_threshold") or int(DEFAULT_CONFIG["automod_phash_threshold"])
        existing = await _find_best_match(phash, threshold)
        if existing is not None:
            entry, distance = existing
            await ctx.send(
                f"{emoji.WARNING} A very similar image is already banned "
                f"(`{entry['hash']}`, distance {distance}) — not adding a duplicate."
            )
            return

        phash_str = _hash_to_str(phash)
        await db.add_banned_image(phash_str, seconds, ctx.author.id, filename=att.filename)
        await ctx.send(
            f"{emoji.SUCCESS} Banned that image. Anyone who sends it (or a close visual match) now "
            f"gets timed out for **{format_duration(seconds)}** and the message deleted.\n"
            f"Hash: `{phash_str}`"
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
            await ctx.send(f"{emoji.DELETE} Un-banned image `{removed}`.")
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

        phash = await _phash_attachment(images[0])
        if phash is None:
            await ctx.send(f"{emoji.ERROR} Couldn't read that as an image — try again.")
            return

        threshold = cfg_int("automod_phash_threshold") or int(DEFAULT_CONFIG["automod_phash_threshold"])
        match = await _find_best_match(phash, threshold)
        if match is None:
            await ctx.send(f"{emoji.WARNING} That image isn't currently banned.")
            return

        entry, _ = match
        await db.remove_banned_image(entry["hash"])
        await ctx.send(f"{emoji.DELETE} Un-banned that image (`{entry['hash']}`).")

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
                name=f"`{entry['hash']}`",
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

    @bot.command(name="automodlogchannel")
    @is_owner()
    async def automodlogchannel(ctx):
        """-automodlogchannel — owner-only. Shows the currently configured
        automod log channel (or that none is set)."""
        channel_id = cfg_int("automod_log_channel_id")
        channel = bot.get_channel(channel_id) if channel_id else None
        if channel:
            await ctx.send(f"{emoji.BANNED_IMAGE} Current automod log channel: {channel.mention}")
        else:
            await ctx.send(
                f"{emoji.BANNED_IMAGE} No automod log channel set yet. "
                f"Use `-setautomodlogchannel #channel` to set one."
            )

    @bot.command(name="setautomodthreshold")
    @is_owner()
    async def setautomodthreshold(ctx, value: int):
        """-setautomodthreshold <0-64> — owner-only. How visually close an
        image needs to be to a banned one to count as a match (perceptual
        hash Hamming distance). Lower = stricter/fewer false positives,
        higher = catches more edits/recolors but risks false positives.
        Default 5 (roughly 8% of the fingerprint)."""
        if not 0 <= value <= PHASH_SIZE * PHASH_SIZE:
            await ctx.send(f"{emoji.WARNING} Must be between 0 and {PHASH_SIZE * PHASH_SIZE}.")
            return
        config["automod_phash_threshold"] = str(value)
        await db.set_config("automod_phash_threshold", value)
        await ctx.send(f"{emoji.SUCCESS} Automod match threshold set to {value}.")

    @bot.command(name="automodthreshold")
    @is_owner()
    async def automodthreshold(ctx):
        """-automodthreshold — owner-only. Shows the current match
        threshold."""
        await ctx.send(
            f"{emoji.BANNED_IMAGE} Current match threshold: "
            f"{cfg_int('automod_phash_threshold') or DEFAULT_CONFIG['automod_phash_threshold']}"
        )

    return (
        banimage, unbanimage, listbannedimages,
        setautomodlogchannel, automodlogchannel,
        setautomodthreshold, automodthreshold,
    )
