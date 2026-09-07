import time
import discord

import db
import webhook

DEFAULT_CONFIG = {
    "vanity_text": "",
    "role_id": "0",
    "log_channel_id": "0",
    "guild_id": "0",
}

# In-memory cache of config, loaded from DB at startup and kept in sync
# whenever an admin command updates it. Values are always strings here;
# cast to int where needed via cfg_int().
config = dict(DEFAULT_CONFIG)

# Reward cycle: hold vanity for this many seconds (cumulative, pausing
# whenever vanity is removed and resuming from the same point when it's
# added back) to earn a JC reward from Jarvis. Hardcoded per request —
# edit these two constants directly to change the cycle.
REWARD_THRESHOLD_SECONDS = 12 * 60 * 60  # 12 hours
REWARD_AMOUNT_JC = 1000


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
    vanity_text = config.get("vanity_text") or "your vanity link"

    if added:
        embed = discord.Embed(
            title="✅ Vanity Added — Role Given",
            description=f'**{member}** added "{vanity_text}" to their status.',
            color=discord.Color.green(),
        )
        embed.add_field(name="👥 User", value=f"{member.mention} (`{member}`)", inline=False)
        embed.add_field(name="🎖️ Role Given", value=role_mention, inline=False)
    else:
        embed = discord.Embed(
            title="❌ Vanity Removed — Role Taken",
            description=f'**{member}** removed "{vanity_text}" from their status.',
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


async def send_reward_embed(bot, member: discord.Member, amount: int, delivered: bool):
    channel_id = cfg_int("log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    if delivered:
        embed = discord.Embed(
            title="🎉 24h Vanity Reward",
            description=f"**{member}** completed a full 24h vanity cycle and earned **{amount:,} JC**!",
            color=discord.Color.gold(),
        )
    else:
        embed = discord.Embed(
            title="⚠️ 24h Vanity Reward — Delivery Failed",
            description=(
                f"**{member}** completed a 24h vanity cycle, but Jarvis didn't "
                f"accept the reward call. Check `JARVIS_WEBHOOK_URL`/`JARVIS_WEBHOOK_SECRET`."
            ),
            color=discord.Color.orange(),
        )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"User ID: {member.id}")
    embed.timestamp = discord.utils.utcnow()
    await channel.send(embed=embed)


async def dm_reward_success(member: discord.Member, amount: int) -> bool:
    """DM the user congratulating them on the reward. Returns False (and
    stays silent otherwise) if their DMs are closed — that's common and
    not worth alarming anyone in the log channel over."""
    vanity_text = config.get("vanity_text") or "your vanity link"
    # Quotes instead of backticks: a backtick around an EMPTY vanity_text
    # leaves Discord's markdown with only one real delimiter, so it pairs
    # up with the next unrelated backtick later in the message (the one
    # around !balance) and swallows everything between them into one
    # giant inline-code block. Quotes don't have that failure mode.
    embed = discord.Embed(
        title="🎉 24h Vanity Reward!",
        description=(
            f'You kept "{vanity_text}" in your status for a full 24 hours '
            f"and earned **{amount:,} JC**! It's already in your Jarvis balance — "
            f"check with !balance.\n\nKeep it up — your next 24h cycle just started."
        ),
        color=discord.Color.gold(),
    )
    try:
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def grant_cycle_reward(bot, member: discord.Member, amount: int = None):
    amount = REWARD_AMOUNT_JC if amount is None else amount
    delivered = await webhook.send_jc_reward(member.id, amount, reason="vanity_24h")
    await send_reward_embed(bot, member, amount, delivered)
    if delivered:
        await dm_reward_success(member, amount)


async def apply_vanity_added(bot, member: discord.Member):
    udata = await db.get_user(member.id)
    if udata["active"]:
        return  # already tracked as active, avoid double-trigger

    session_start = time.time()
    await db.upsert_user(member.id, True, session_start, udata["total_seconds"], udata["cycle_seconds"])

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
    new_cycle = udata["cycle_seconds"] + session_seconds

    # The cycle pauses right here — new_cycle is saved as-is (not reset),
    # so whenever this user re-adds the vanity link, apply_vanity_added()
    # picks the cycle back up from this exact value.
    rewarded = False
    while new_cycle >= REWARD_THRESHOLD_SECONDS:
        new_cycle -= REWARD_THRESHOLD_SECONDS
        rewarded = True

    await db.upsert_user(member.id, False, None, new_total, new_cycle)

    role = member.guild.get_role(cfg_int("role_id"))
    if role and role in member.roles:
        try:
            await member.remove_roles(role, reason="Vanity link removed from status")
        except discord.Forbidden:
            pass

    await send_log_embed(bot, member, added=False, session_seconds=session_seconds, total_seconds=new_total)

    if rewarded:
        await grant_cycle_reward(bot, member)


async def check_cycle_progress(bot):
    """Periodic check so a reward fires even if someone never removes their
    vanity — without this, a continuously-active user would only get
    rewarded the next time apply_vanity_removed() happens to run, which
    might be days later."""
    guild_id = cfg_int("guild_id")
    now = time.time()

    active_users = await db.get_active_users()
    for udata in active_users:
        session_start = udata["session_start"] or now
        elapsed = now - session_start
        prospective_cycle = udata["cycle_seconds"] + elapsed

        if prospective_cycle < REWARD_THRESHOLD_SECONDS:
            continue  # hasn't hit the threshold yet, nothing to do

        # Roll the checkpoint: fold the elapsed time into total/cycle now
        # (rather than waiting for removal), reset session_start to "now"
        # so we don't double-count this elapsed span next time, and carry
        # any overflow past the threshold into the next cycle.
        new_total = udata["total_seconds"] + elapsed
        new_cycle = prospective_cycle
        rewards_earned = 0
        while new_cycle >= REWARD_THRESHOLD_SECONDS:
            new_cycle -= REWARD_THRESHOLD_SECONDS
            rewards_earned += 1

        await db.upsert_user(udata["user_id"], True, now, new_total, new_cycle)

        guild = bot.get_guild(guild_id) if guild_id else (bot.guilds[0] if bot.guilds else None)
        member = guild.get_member(udata["user_id"]) if guild else None
        if member is None:
            continue

        for _ in range(rewards_earned):
            await grant_cycle_reward(bot, member)


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
                await db.upsert_user(member.id, True, time.time(), udata["total_seconds"], udata["cycle_seconds"])
            elif not currently_has and udata["active"]:
                # they had it before a restart but not anymore; close out silently
                await db.upsert_user(member.id, False, None, udata["total_seconds"], udata["cycle_seconds"])
