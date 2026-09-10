"""
cogs/auto_quests.py — Auto-completing daily quests
====================================================

A SEPARATE, simpler system from cards.py's /quests (card quests, manual
claim, card reward). This one:

  - Assigns ONE quest per user per calendar day (UTC).
  - Tracks progress automatically in the background — no command needed.
  - The moment the target is hit, auto-grants JC via state.add_credits()
    and posts a public "Daily Quest Completed" embed to a configured log
    channel — no claim button, matching the reference bot's behaviour.
  - Immediately assigns a fresh quest afterward so tracking continues
    seamlessly into the next day.

QUEST_DEFS is deliberately just the two examples requested for now — add,
remove, or re-tune quests here freely; nothing else needs to change to
support a new quest type as long as it has a "get" function that returns
a monotonically-increasing counter for a user.

Owner commands:
  !setquestlogchannel #channel  — where completion embeds get posted
  !questlogchannel               — check what's currently configured

Caveats worth knowing about before relying on this:
  - "Send 150 Messages" counts every message a user sends in any guild
    the bot can see (tracked via state.increment_server_message_count(),
    a dedicated counter separate from the AI-chat-only one used by the
    existing /quests card system).
  - "Bump the Server" detection assumes Disboard (bot ID 302050872383242240)
    and relies on message.interaction / message.interaction_metadata to
    identify who ran the /bump slash command. This is the standard way to
    detect it, but if your server uses a different bump bot, update
    DISBOARD_BOT_ID and _extract_bump_user() below.
"""
import os

import discord
from discord.ext import commands, tasks

from cogs.state import (
    get_bump_count, increment_bump_count,
    get_server_message_count, increment_server_message_count,
    get_auto_quest, assign_auto_quest, mark_auto_quest_rewarded,
    add_credits, get_setting, set_setting,
)

JC_EMOJI = "🪙"
JC_NAME = "Jarvis Credit"

DISBOARD_BOT_ID = 302050872383242240

# Add/edit/remove quest types here. "get" must return a number that only
# ever goes up for that user (a lifetime counter) — progress is measured
# as get(uid) - baseline, where baseline is snapshotted the moment the
# quest is assigned.
QUEST_DEFS: dict[str, dict] = {
    "chat150": {
        "desc": "Send 150 Messages",
        "target": 150,
        "reward": 100_000,
        "get": lambda uid: get_server_message_count(uid),
    },
    "bump_server": {
        "desc": "Bump the Server",
        "target": 1,
        "reward": 15_000,
        "get": lambda uid: get_bump_count(uid),
    },
}

QUEST_LOG_CHANNEL_SETTING_KEY = "auto_quest_log_channel_id"


def _quest_progress(user_id: int, entry: dict) -> int:
    qdef = QUEST_DEFS.get(entry["quest_id"])
    if qdef is None:
        return 0  # quest_id no longer exists (removed from QUEST_DEFS) — treat as stuck at 0
    current = qdef["get"](user_id)
    return max(0, min(qdef["target"], current - entry["baseline"]))


class AutoQuests(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_loop.start()

    def cog_unload(self):
        self.check_loop.cancel()

    # ── Quest assignment / completion ───────────────────────────────────

    def _ensure_quest(self, user_id: int) -> dict:
        """Return the user's current quest, assigning a fresh one
        (random pick from QUEST_DEFS, new baseline) if they don't have
        one yet for today."""
        entry = get_auto_quest(user_id)
        if entry is not None:
            return entry

        import random
        quest_id = random.choice(list(QUEST_DEFS.keys()))
        baseline = QUEST_DEFS[quest_id]["get"](user_id)
        return assign_auto_quest(user_id, quest_id, baseline)

    async def _check_and_reward(self, member: discord.Member):
        """Call after any event that might have completed member's quest
        (a message counted, a bump detected, ...). Safe to call often —
        it's a no-op unless the quest is actually freshly complete."""
        entry = self._ensure_quest(member.id)
        if entry["rewarded"]:
            return

        qdef = QUEST_DEFS.get(entry["quest_id"])
        if qdef is None:
            return

        if _quest_progress(member.id, entry) < qdef["target"]:
            return  # not done yet

        # Completed! Pay out, log it, mark done, then immediately line up
        # the next quest so tomorrow (or right now, if they keep going)
        # tracking continues without any gap.
        reward = qdef["reward"]
        new_balance = add_credits(member.id, reward)
        mark_auto_quest_rewarded(member.id)

        await self._post_completion_log(member, qdef["desc"], reward, new_balance)

        # Line up the next quest right away (fresh baseline) rather than
        # waiting for the day to roll over — matches the reference bot,
        # which shows multiple different quests completing per user per day.
        import random
        next_quest_id = random.choice(list(QUEST_DEFS.keys()))
        next_baseline = QUEST_DEFS[next_quest_id]["get"](member.id)
        assign_auto_quest(member.id, next_quest_id, next_baseline)

    async def _post_completion_log(self, member: discord.Member, quest_desc: str, reward: int, new_balance: int):
        channel_id = get_setting(QUEST_LOG_CHANNEL_SETTING_KEY, 0)
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        embed = discord.Embed(title="🎉 Daily Quest Completed", color=discord.Color.green())
        embed.add_field(name="👥 User", value=f"{member.mention} (`{member}`)", inline=False)
        embed.add_field(name="🌸 Quest", value=quest_desc, inline=False)
        embed.add_field(name="🎁 Reward", value=f"{reward:,} {JC_EMOJI} {JC_NAME}s", inline=False)
        embed.add_field(name="💰 New Balance", value=f"{new_balance:,} {JC_EMOJI} {JC_NAME}s", inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ── Message-based quest progress (chat150) ──────────────────────────
    # Every non-bot message in a guild bumps server_message_counts via
    # increment_server_message_count() below, then we check for completion.

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.author.id == DISBOARD_BOT_ID:
            bumper = self._extract_bump_user(message)
            if bumper is not None:
                increment_bump_count(bumper.id)
                await self._check_and_reward(bumper)
            return

        if message.author.bot:
            return

        # Count this message toward "Send N Messages" (any message,
        # anywhere in the server — not just AI chat), then check whether
        # that just completed their current quest.
        increment_server_message_count(message.author.id)
        await self._check_and_reward(message.author)

    @staticmethod
    def _extract_bump_user(message: discord.Message) -> discord.Member | None:
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

    # ── Periodic safety-net check ────────────────────────────────────────
    # Message-based progress is checked live in on_message, but a quest
    # could also be "secretly" already complete right when it's assigned
    # (e.g. baseline math edge cases) — this loop catches anything missed.

    @tasks.loop(minutes=5)
    async def check_loop(self):
        for guild in self.bot.guilds:
            for member in guild.members:
                if member.bot:
                    continue
                entry = get_auto_quest(member.id)
                if entry is not None and not entry["rewarded"]:
                    await self._check_and_reward(member)

    @check_loop.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

    # ── Owner config commands ────────────────────────────────────────────

    @commands.command(name="setquestlogchannel")
    @commands.is_owner()
    async def setquestlogchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        set_setting(QUEST_LOG_CHANNEL_SETTING_KEY, channel.id)
        await ctx.send(f"✅ Daily quest completions will now be logged in {channel.mention}.")

    @commands.command(name="questlogchannel")
    @commands.is_owner()
    async def questlogchannel(self, ctx: commands.Context):
        channel_id = get_setting(QUEST_LOG_CHANNEL_SETTING_KEY, 0)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        await ctx.send(f"📋 Current quest log channel: {channel.mention if channel else 'not set'}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoQuests(bot))
