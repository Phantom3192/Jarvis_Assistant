"""
boxes.py — Random JC box drops while chatting
================================================

Each eligible message has a small chance to drop a surprise box worth
0–1000 JC. Eligible means:

  - at least 2h of vanity time today (same gate as quest claiming — see
    vanity.get_today_vanity_seconds()), and
  - at least BOX_COOLDOWN_SECONDS (12h) since that user's last drop.

The cooldown only resets on an actual drop — messages that fail the
random roll (or the vanity/cooldown gates) don't touch it, so someone who
was eligible five minutes ago is still eligible now.

On a hit the amount is sent to Jarvis over the same webhook vanity
rewards use, then a log embed + DM go out — same pattern as the vanity
cycle reward and quest claims.
"""
import random
import time

import discord

import db
import vanity
import webhook
import quests

JC_EMOJI = "🪙"
JC_NAME = "Jarvis Credit"

BOX_COOLDOWN_SECONDS = 12 * 60 * 60  # 12h between drops per user
BOX_DROP_CHANCE = 0.05  # chance per eligible message that a box drops
REQUIRED_VANITY_SECONDS = 2 * 60 * 60  # 2h vanity today, required to get a box
BOX_MIN_JC = 0
BOX_MAX_JC = 1000


async def maybe_drop(bot, member: discord.Member) -> None:
    """Call on every non-bot guild message. No-op unless the user is off
    cooldown, has today's vanity requirement met, and the random roll
    actually hits."""
    if member.bot:
        return

    last_drop = await db.get_last_box_drop(member.id)
    if time.time() - last_drop < BOX_COOLDOWN_SECONDS:
        return

    vanity_seconds = await vanity.get_today_vanity_seconds(member.id)
    if vanity_seconds < REQUIRED_VANITY_SECONDS:
        return

    if random.random() >= BOX_DROP_CHANCE:
        return

    # Cooldown starts now regardless of the rolled amount (even a 0 JC
    # box still counts as "found one" and gates the next 12h).
    await db.set_last_box_drop(member.id, time.time())

    # Opening the box counts toward the "Open 1 Box" quest regardless of
    # the JC amount rolled.
    await db.increment_quest_box_count(member.id)
    await quests.check_and_complete(bot, member)

    amount = random.randint(BOX_MIN_JC, BOX_MAX_JC)
    delivered = True
    if amount > 0:
        delivered = await webhook.send_jc_reward(member.id, amount, reason="random_box")

    await _post_log(bot, member, amount, delivered)
    if delivered and amount > 0:
        await _dm_success(member, amount)


async def _post_log(bot, member: discord.Member, amount: int, delivered: bool) -> None:
    channel_id = vanity.cfg_int("quest_log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    if not delivered:
        embed = discord.Embed(
            title="⚠️ Random Box — Delivery Failed",
            description=(
                f"**{member}** found a box worth **{amount:,} JC**, but Jarvis "
                f"didn't accept the reward call."
            ),
            color=discord.Color.orange(),
        )
    else:
        embed = discord.Embed(
            title="🎁 Random Box Found!",
            description=f"**{member}** stumbled on a surprise box worth **{amount:,} {JC_EMOJI} {JC_NAME}s**!",
            color=discord.Color.gold(),
        )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"User ID: {member.id}")
    embed.timestamp = discord.utils.utcnow()
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


async def _dm_success(member: discord.Member, amount: int) -> None:
    embed = discord.Embed(
        title="🎁 Random Box Found!",
        description=(
            f"You stumbled on a surprise box worth **{amount:,} JC**! "
            f"It's already in your Jarvis balance — check with !balance."
        ),
        color=discord.Color.gold(),
    )
    try:
        await member.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass
