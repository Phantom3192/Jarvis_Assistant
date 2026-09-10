"""
quests.py — Daily quests that stay local until claimed
========================================================

ALL quest types in QUEST_DEFS are active for every user at once and are
tracked/completed/claimed fully independently of each other — there is
no more "one random quest per day".

A quest completing does NOT push a reward to Jarvis by itself. Instead:

  1. Progress for every quest type is tracked automatically in the
     background, entirely inside this bot's own DB (quest_counters /
     quest_progress in db.py) — Jarvis never sees any of this until
     step 3.
  2. The moment a quest's target is hit, that quest is marked
     "completed" here only, and a heads-up gets posted to the quest log
     channel.
  3. The user has to run -claimquest themselves (optionally naming a
     specific quest, or with no argument to claim everything that's
     ready at once). Claiming only succeeds if they've also kept their
     vanity status up for at least REQUIRED_VANITY_SECONDS *today* (UTC
     calendar day) — see vanity.get_today_vanity_seconds(). Only then
     does the reward get sent to Jarvis over the same webhook vanity
     rewards use.

Each quest type resets independently at the UTC day boundary (00:00
UTC) — a fresh baseline gets snapshotted the next time progress is
checked for that quest type. If a quest completes but isn't claimed
before the day rolls over, it's gone — this is deliberate: the
2h-vanity-today requirement is meant to gate *that day's* quests, not
bank indefinitely.
"""
import discord

import db
import vanity
import webhook
import emoji

JC_EMOJI = emoji.JC
JC_NAME = emoji.JC_NAME

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


async def _ensure_quests(user_id: int) -> dict:
    """Return {quest_id: entry} covering every quest type in QUEST_DEFS
    for today, assigning a fresh baseline for any quest type the user
    doesn't have an up-to-date (today-dated) row for yet."""
    today = db.today_str()
    existing = await db.get_all_quests(user_id)

    result = {}
    counters = None  # fetched lazily, only if something actually needs (re)assigning
    for quest_id, qdef in QUEST_DEFS.items():
        entry = existing.get(quest_id)
        if entry is not None and entry["assigned_date"] == today:
            result[quest_id] = entry
            continue

        if counters is None:
            counters = await db.get_quest_counters(user_id)
        baseline = qdef["get"](counters)
        result[quest_id] = await db.assign_quest_progress(user_id, quest_id, baseline, today)

    return result


def _quest_progress(entry: dict, qdef: dict, counters: dict) -> int:
    current = qdef["get"](counters)
    return max(0, min(qdef["target"], current - entry["baseline"]))


async def check_and_complete(bot, member: discord.Member) -> None:
    """Call after any event that might have completed one of member's
    quests (a message counted, a bump detected, ...). Safe to call often
    — a no-op for any quest that isn't actually freshly complete. Marks
    finished quests completed and posts a claimable heads-up per quest;
    does NOT reward — that only happens via -claimquest."""
    entries = await _ensure_quests(member.id)
    counters = await db.get_quest_counters(member.id)

    for quest_id, entry in entries.items():
        if entry["completed"]:
            continue
        qdef = QUEST_DEFS[quest_id]
        if _quest_progress(entry, qdef, counters) < qdef["target"]:
            continue  # not done yet

        await db.mark_quest_progress_completed(member.id, quest_id)
        await _post_ready_log(bot, member, qdef["desc"])


async def _post_ready_log(bot, member: discord.Member, quest_desc: str) -> None:
    channel_id = vanity.cfg_int("quest_log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    embed = discord.Embed(
        title=f"{emoji.SUCCESS} Quest Ready to Claim",
        description=f"{member.mention} finished **{quest_desc}** — run `-claimquest` to collect it.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=f"{emoji.LOCKED} Requirement",
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

    embed = discord.Embed(title=f"{emoji.CELEBRATE} Quest Claimed", color=discord.Color.green())
    embed.add_field(name=f"{emoji.USER} User", value=f"{member.mention} (`{member}`)", inline=False)
    embed.add_field(name=f"{emoji.QUEST_TYPE} Quest", value=quest_desc, inline=False)
    embed.add_field(name=f"{emoji.GIFT} Reward", value=f"{reward:,} {JC_EMOJI} {JC_NAME}s", inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


async def status_embed(member: discord.Member) -> discord.Embed:
    """Embed for the -quest command — every quest type and its current
    progress/claim status, all shown at once."""
    entries = await _ensure_quests(member.id)
    counters = await db.get_quest_counters(member.id)
    vanity_seconds = await vanity.get_today_vanity_seconds(member.id)

    embed = discord.Embed(
        title=f"{emoji.QUEST_LIST} Daily Quests",
        description=f"{member.mention}'s quests for today — all reset at 00:00 UTC.",
        color=discord.Color.blurple(),
    )
    for quest_id, qdef in QUEST_DEFS.items():
        entry = entries[quest_id]
        if entry["claimed"]:
            value = f"{emoji.SUCCESS} Claimed today — come back tomorrow."
        elif entry["completed"]:
            if vanity_seconds >= REQUIRED_VANITY_SECONDS:
                value = f"{emoji.GIFT} Ready! Run `-claimquest {quest_id}` (or `-claimquest`) to collect {qdef['reward']:,} {JC_EMOJI}."
            else:
                remaining = REQUIRED_VANITY_SECONDS - vanity_seconds
                value = f"{emoji.GIFT} Done, but needs {vanity.format_duration(remaining)} more vanity time today to claim."
        else:
            prog = _quest_progress(entry, qdef, counters)
            value = f"{prog}/{qdef['target']} — reward: {qdef['reward']:,} {JC_EMOJI}"
        embed.add_field(name=qdef["desc"], value=value, inline=False)

    embed.set_thumbnail(url=member.display_avatar.url)
    return embed


async def claim(bot, member: discord.Member, quest_id: str | None = None) -> str:
    """Claim member's completed quest(s). With quest_id, claims just that
    one; with none, claims every quest that's currently completed and
    unclaimed. Returns a chat-ready message describing the outcome."""
    entries = await _ensure_quests(member.id)

    if quest_id is not None:
        quest_id = quest_id.lower()
        if quest_id not in QUEST_DEFS:
            valid = ", ".join(f"`{q}`" for q in QUEST_DEFS)
            return f"{emoji.WARNING} Unknown quest `{quest_id}`. Valid quest IDs: {valid}"
        targets = [quest_id]
    else:
        targets = list(QUEST_DEFS.keys())

    counters = await db.get_quest_counters(member.id)
    ready = []  # [(quest_id, qdef), ...] — completed and not yet claimed
    for qid in targets:
        entry = entries[qid]
        qdef = QUEST_DEFS[qid]

        if entry["claimed"]:
            continue
        if not entry["completed"]:
            if quest_id is not None:
                prog = _quest_progress(entry, qdef, counters)
                return f"{emoji.PENDING} Quest not finished yet: **{qdef['desc']}** ({prog}/{qdef['target']})."
            continue
        ready.append((qid, qdef))

    if not ready:
        if quest_id is not None:
            return f"{emoji.SUCCESS} You've already claimed **{QUEST_DEFS[quest_id]['desc']}** today."
        return f"{emoji.QUEST_LIST} Nothing ready to claim right now — check `-quest` to see your progress."

    vanity_seconds = await vanity.get_today_vanity_seconds(member.id)
    if vanity_seconds < REQUIRED_VANITY_SECONDS:
        remaining = REQUIRED_VANITY_SECONDS - vanity_seconds
        return (
            f"{emoji.LOCKED} You need to keep your vanity status up for at least "
            f"{vanity.format_duration(REQUIRED_VANITY_SECONDS)} today to claim quests. "
            f"You're at {vanity.format_duration(vanity_seconds)} — "
            f"{vanity.format_duration(remaining)} to go."
        )

    lines = []
    for qid, qdef in ready:
        reward = qdef["reward"]
        delivered = await webhook.send_jc_reward(member.id, reward, reason=f"quest:{qid}")
        if not delivered:
            lines.append(f"{emoji.WARNING} **{qdef['desc']}** — Jarvis didn't accept the reward call, try again shortly.")
            continue
        await db.mark_quest_progress_claimed(member.id, qid)
        await _post_claim_log(bot, member, qdef["desc"], reward)
        lines.append(f"{emoji.CELEBRATE} Claimed **{qdef['desc']}** — {reward:,} {JC_EMOJI} {JC_NAME}s sent to your Jarvis balance!")

    return "\n".join(lines)


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
