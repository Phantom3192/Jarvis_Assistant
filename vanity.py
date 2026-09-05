import time
import discord

import db

DEFAULT_CONFIG = {
    "vanity_text": ".gg/mysticverse",
    "role_id": "0",
    "log_channel_id": "0",
    "guild_id": "0",
}

# In-memory cache of config, loaded from DB at startup and kept in sync
# whenever an admin command updates it. Values are always strings here;
# cast to int where needed via cfg_int().
config = dict(DEFAULT_CONFIG)


def cfg_int(key: str) -> int:
    try:
        return int(config.get(key, 0))
    except (TypeError, ValueError):
        return 0


def format_duration(total_seconds: float) -> str:
    total_seconds = int(total_seconds)
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def has_vanity(member: discord.Member) -> bool:
    vanity_text = config.get("vanity_text", "").lower()
    if not vanity_text or member is None:
        return False
    for activity in member.activities:
        if isinstance(activity, discord.CustomActivity) and activity.name:
            if vanity_text in activity.name.lower():
                return True
    return False


async def send_log_embed(bot, member: discord.Member, added: bool, session_seconds, total_seconds: float):
    channel_id = cfg_int("log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    role = member.guild.get_role(cfg_int("role_id"))
    role_mention = role.mention if role else "*(role not configured)*"

    if added:
        embed = discord.Embed(
            title="✅ Vanity Added — Role Given",
            description=f"**{member}** added `{config['vanity_text']}` to their status.",
            color=discord.Color.green(),
        )
        embed.add_field(name="👥 User", value=f"{member.mention} (`{member}`)", inline=False)
        embed.add_field(name="🎖️ Role Given", value=role_mention, inline=False)
    else:
        embed = discord.Embed(
            title="❌ Vanity Removed — Role Taken",
            description=f"**{member}** removed `{config['vanity_text']}` from their status.",
            color=discord.Color.red(),
        )
        embed.add_field(name="👥 User", value=f"{member.mention} (`{member}`)", inline=False)
        embed.add_field(name="❌ Role Taken", value=role_mention, inline=True)
        embed.add_field(name="⏱️ Session Duration", value=format_duration(session_seconds or 0), inline=True)
        embed.add_field(name="🎉 Total Vanity Time", value=format_duration(total_seconds), inline=True)

    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"User ID: {member.id}")
    embed.timestamp = discord.utils.utcnow()

    await channel.send(embed=embed)


async def apply_vanity_added(bot, member: discord.Member):
    udata = await db.get_user(member.id)
    if udata["active"]:
        return  # already tracked as active, avoid double-trigger

    session_start = time.time()
    await db.upsert_user(member.id, True, session_start, udata["total_seconds"])

    role = member.guild.get_role(cfg_int("role_id"))
    if role and role not in member.roles:
        try:
            await member.add_roles(role, reason="Vanity link detected in status")
        except discord.Forbidden:
            pass

    await send_log_embed(bot, member, added=True, session_seconds=None, total_seconds=udata["total_seconds"])


async def apply_vanity_removed(bot, member: discord.Member):
    udata = await db.get_user(member.id)
    if not udata["active"]:
        return

    session_start = udata["session_start"] or time.time()
    session_seconds = time.time() - session_start
    new_total = udata["total_seconds"] + session_seconds

    await db.upsert_user(member.id, False, None, new_total)

    role = member.guild.get_role(cfg_int("role_id"))
    if role and role in member.roles:
        try:
            await member.remove_roles(role, reason="Vanity link removed from status")
        except discord.Forbidden:
            pass

    await send_log_embed(bot, member, added=False, session_seconds=session_seconds, total_seconds=new_total)


async def load_config_from_db():
    stored = await db.get_all_config()
    for key, value in stored.items():
        config[key] = value


async def initial_scan(bot):
    """Re-sync active vanity state for all cached members after a restart."""
    guild_id = cfg_int("guild_id")
    guilds = [bot.get_guild(guild_id)] if guild_id else bot.guilds
    for guild in guilds:
        if guild is None:
            continue
        for member in guild.members:
            if member.bot:
                continue
            udata = await db.get_user(member.id)
            currently_has = has_vanity(member)
            if currently_has and not udata["active"]:
                await db.upsert_user(member.id, True, time.time(), udata["total_seconds"])
            elif not currently_has and udata["active"]:
                # they had it before a restart but not anymore; close out silently
                await db.upsert_user(member.id, False, None, udata["total_seconds"])
