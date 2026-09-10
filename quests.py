"""
quests.py — Daily quests that stay local until claimed
========================================================

A quest completing does NOT push a reward to Jarvis by itself. Instead:

  1. Progress is tracked automatically in the background, entirely inside
     this bot's own DB (quest_counters / quest_data in db.py) — Jarvis
     never sees any of this until step 3.
  2. The moment the target is hit, the quest is marked "completed" here
     only, and a heads-up gets posted to the quest log channel.
  3. The user has to run -claimquest themselves. Claiming only succeeds
     if they've also kept their vanity status up for at least
     REQUIRED_VANITY_SECONDS *today* (UTC calendar day) — see
     vanity.get_today_vanity_seconds(). Only then does the reward get
     sent to Jarvis over the same webhook vanity rewards use.
  4. A successful claim immediately lines up a new quest so tracking
     continues without a gap.

One quest per user per UTC day. If a quest completes but isn't claimed
before the day rolls over, it's gone — a fresh one gets assigned instead
next time progress is checked. This is deliberate: the 2h-vanity-today
requirement is meant to gate *that day's* quest, not bank indefinitely.
"""
import random

import discord

import db
import vanity
import webhook

JC_EMOJI = "🪙"
JC_NAME = "Jarvis Credit"

REQUIRED_VANITY_SECONDS = 2 * 60 * 60  # 2h of vanity today, required to claim

DISBOARD_BOT_ID = 302050872383242240

# Add/edit/remove quest types here. "get" must return a number that only
# ever goes up for that user (a lifetime counter, stored in
# quest_counters) — progress is measured as get(counters) - baseline,
# where baseline is snapshotted the moment the quest is assigned.
QUEST_DEFS: dict[str, dict] = {
    "chat150": {
        "desc": "Send 200 Messages",
        "target": 200,
        "reward": 2_000,
        "get": lambda counters: counters["messages"],
    },
    "bump_server": {
        "desc": "Bump the Server",
        "target": 1,
        "reward": 1_000,
        "get": lambda counters: counters["bumps"],
    },
    "open_box": {
        "desc": "Open 1 Box",
        "target": 1,
        "reward": 500,
        "get": lambda counters: counters["boxes"],
    },
}


async def _ensure_quest(user_id: int) -> dict:
    """Return the user's quest for today, assigning a fresh one (random
    pick from QUEST_DEFS, new baseline) if they don't have one yet, or if
    the one on file is from a previous day."""
    entry = await db.get_quest(user_id)
    today = db.today_str()
    if entry is not None and entry["assigned_date"] == today:
        return entry

    counters = await db.get_quest_counters(user_id)
    quest_id = random.choice(list(QUEST_DEFS.keys()))
    baseline = QUEST_DEFS[quest_id]["get"](counters)
    return await db.assign_quest(user_id, quest_id, baseline, today)


def _quest_progress(entry: dict, counters: dict) -> int:
    qdef = QUEST_DEFS.get(entry["quest_id"])
    if qdef is None:
        return 0
    current = qdef["get"](counters)
    return max(0, min(qdef["target"], current - entry["baseline"]))


async def check_and_complete(bot, member: discord.Member) -> None:
    """Call after any event that might have completed member's quest (a
    message counted, a bump detected, ...). Safe to call often — a no-op
    unless the quest is actually freshly complete. Marks it completed and
    posts a claimable heads-up; does NOT reward — that only happens via
    -claimquest."""
    entry = await _ensure_quest(member.id)
    if entry["completed"]:
        return

    qdef = QUEST_DEFS.get(entry["quest_id"])
    if qdef is None:
        return

    counters = await db.get_quest_counters(member.id)
    if _quest_progress(entry, counters) < qdef["target"]:
        return  # not done yet

    await db.mark_quest_completed(member.id)
    await _post_ready_log(bot, member, qdef["desc"])


async def _post_ready_log(bot, member: discord.Member, quest_desc: str) -> None:
    channel_id = vanity.cfg_int("quest_log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    embed = discord.Embed(
        title="✅ Quest Ready to Claim",
        description=f"{member.mention} finished **{quest_desc}** — run `-claimquest` to collect it.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🔒 Requirement",
        value=f"Needs {vanity.format_duration(REQUIRED_VANITY_SECONDS)} of vanity time today to claim.",
        inline=False,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


async def _post_claim_log(bot, member: discord.Member, quest_desc: str, reward: int) -> None:
    channel_id = vanity.cfg_int("quest_log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    embed = discord.Embed(title="🎉 Quest Claimed", color=discord.Color.green())
    embed.add_field(name="👥 User", value=f"{member.mention} (`{member}`)", inline=False)
    embed.add_field(name="🌸 Quest", value=quest_desc, inline=False)
    embed.add_field(name="🎁 Reward", value=f"{reward:,} {JC_EMOJI} {JC_NAME}s", inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


async def status_text(member: discord.Member) -> str:
    """Human-readable status for the -quest command."""
    entry = await _ensure_quest(member.id)
    qdef = QUEST_DEFS.get(entry["quest_id"])
    if qdef is None:
        return "⚠️ Your quest is no longer valid — a fresh one will be assigned shortly."

    if entry["claimed"]:
        return f"✅ Today's quest (**{qdef['desc']}**) is already claimed. Come back tomorrow!"

    if entry["completed"]:
        vanity_seconds = await vanity.get_today_vanity_seconds(member.id)
        if vanity_seconds >= REQUIRED_VANITY_SECONDS:
            return f"🎁 **{qdef['desc']}** is done and ready — run `-claimquest` to collect {qdef['reward']:,} {JC_EMOJI}!"
        remaining = REQUIRED_VANITY_SECONDS - vanity_seconds
        return (
            f"🎁 **{qdef['desc']}** is done, but you still need "
            f"{vanity.format_duration(remaining)} more vanity time today before you can claim it."
        )

    counters = await db.get_quest_counters(member.id)
    prog = _quest_progress(entry, counters)
    return f"📋 Today's quest: **{qdef['desc']}** ({prog}/{qdef['target']}) — reward: {qdef['reward']:,} {JC_EMOJI}"


async def claim(bot, member: discord.Member) -> str:
    """Attempt to claim member's completed quest. Returns a chat-ready
    message describing the outcome."""
    entry = await db.get_quest(member.id)
    today = db.today_str()

    if entry is None or entry["assigned_date"] != today:
        return "📋 You don't have a quest in progress today yet — do something to get one assigned!"

    qdef = QUEST_DEFS.get(entry["quest_id"])
    if qdef is None:
        return "⚠️ Your quest is no longer valid — a fresh one will be assigned shortly."

    if entry["claimed"]:
        return "✅ You've already claimed today's quest."

    if not entry["completed"]:
        counters = await db.get_quest_counters(member.id)
        prog = _quest_progress(entry, counters)
        return f"⏳ Quest not finished yet: **{qdef['desc']}** ({prog}/{qdef['target']})."

    vanity_seconds = await vanity.get_today_vanity_seconds(member.id)
    if vanity_seconds < REQUIRED_VANITY_SECONDS:
        remaining = REQUIRED_VANITY_SECONDS - vanity_seconds
        return (
            f"🔒 You need to keep your vanity status up for at least "
            f"{vanity.format_duration(REQUIRED_VANITY_SECONDS)} today to claim this. "
            f"You're at {vanity.format_duration(vanity_seconds)} — "
            f"{vanity.format_duration(remaining)} to go."
        )

    reward = qdef["reward"]
    delivered = await webhook.send_jc_reward(member.id, reward, reason=f"quest:{entry['quest_id']}")
    if not delivered:
        return "⚠️ Everything checked out, but Jarvis didn't accept the reward call — try again in a moment."

    await db.mark_quest_claimed(member.id)
    await _post_claim_log(bot, member, qdef["desc"], reward)

    # Line up the next quest right away (fresh baseline) rather than
    # waiting for the day to roll over.
    counters = await db.get_quest_counters(member.id)
    next_quest_id = random.choice(list(QUEST_DEFS.keys()))
    next_baseline = QUEST_DEFS[next_quest_id]["get"](counters)
    await db.assign_quest(member.id, next_quest_id, next_baseline, today)

    return f"🎉 Claimed **{qdef['desc']}** — {reward:,} {JC_EMOJI} {JC_NAME}s sent to your Jarvis balance!"


# ── Progress tracking (message counting + bump detection) ───────────────
# Wired from main.py's on_message so this module owns all quest logic.

async def on_message_progress(bot, message: discord.Message) -> None:
    if message.guild is None:
        return

    if message.author.id == DISBOARD_BOT_ID:
        bumper = _extract_bump_user(message)
        if bumper is not None:
            await db.increment_quest_bump_count(bumper.id)
            await check_and_complete(bot, bumper)
        return

    if message.author.bot:
        return

    await db.increment_quest_message_count(message.author.id)
    await check_and_complete(bot, message.author)


def _extract_bump_user(message: discord.Message):
    """Best-effort: figure out who ran the /bump slash command that
    produced this Disboard confirmation message. Modern discord.py
    exposes this via message.interaction_metadata (2.4+) or the older
    message.interaction (pre-2.4) — try both."""
    meta = getattr(message, "interaction_metadata", None)
    if meta is not None and getattr(meta, "user", None) is not None:
        return meta.user
    interaction = getattr(message, "interaction", None)
    if interaction is not None and getattr(interaction, "user", None) is not None:
        return interaction.user
    return None
