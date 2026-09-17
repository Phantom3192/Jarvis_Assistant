"""
giveaways.py — Auto-reward JC giveaway winners (Giveaway Boat integration)
============================================================================

Giveaway Boat posts a "Congratulations! 🎉" embed in the giveaway channel
when a giveaway ends, e.g.:

    @Spidey , @Satoru Gojo won the giveaway of **2000 JC**!

    • Hosted by: @Phantom || Developer
    • Reroll Command: `g.reroll 1549885899389538457`

check_message() watches for that embed, and if the prize is expressed as
a plain "<amount> JC" (case-insensitive — anything else, e.g. Nitro or a
physical prize, is left alone since we can't auto-reward it), it:

  1. Pulls every winner mention out of the embed text (works for however
     many winners a giveaway has).
  2. Sends each winner the JC reward via webhook.send_jc_reward() —
     same path vanity/quest rewards use to reach Jarvis's economy.
  3. Posts a "reward distributed" confirmation in the same channel.
     No DMs — the confirmation in-channel is the only notification.

Each Giveaway Boat message is only ever processed once (tracked by
message ID in the DB), so a bot restart or Discord redelivering the
gateway event can't double-pay a giveaway.

Reward convention: each winner listed receives the FULL stated amount
(a "giveaway of 2000 JC" with 2 winners pays each winner 2000 JC, not
1000 JC split between them) — that matches how Giveaway Boat's own
winner count works (each draw is an independent winner of the same
prize). Flip GIVEAWAY_SPLIT_AMONG_WINNERS below if you'd rather split it.

Wired up from main.py via setup(bot, owner_id); check_message(bot,
message) is called from on_message, same pattern as automod.py's
check_message; load_config_from_db() is called once at startup.
"""
import re

import discord
from discord.ext import commands

import db
import emoji
import webhook

# Set True to divide the stated prize evenly across winners instead of
# giving each winner the full amount.
GIVEAWAY_SPLIT_AMONG_WINNERS = False

# Matches "... of **2,000 JC**!" (or "2000 jc", any casing/spacing) —
# only a plain-number-of-JC prize is auto-rewardable.
_JC_PRIZE_RE = re.compile(r"of\s+\*{0,2}([\d,]+)\s*jc\*{0,2}", re.IGNORECASE)
_MENTION_RE = re.compile(r"<@!?(\d+)>")

# Giveaway Boat's own user ID, and no channel restriction, by default —
# both overridable via -setgiveawaybot / -setgiveawaychannel.
DEFAULT_CONFIG = {"giveaway_bot_id": "530082442967646230", "giveaway_channel_id": "0"}
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


def _collect_component_text(components) -> list[str]:
    """Recursively pulls text out of a Components V2 layout (Container /
    Section / ActionRow nest their contents in `.children`; the actual
    text lives on `TextDisplay.content`). Giveaway Boat's "GIVEAWAY
    ENDED" / "Congratulations!" result messages are sent this way, not
    as legacy embeds — a CV2 message has `flags.IsComponentsV2` set,
    which means Discord disables `content` and `embeds` on it entirely,
    so `message.embeds` and `message.content` are both empty even
    though the message clearly has text when you look at it."""
    texts = []
    for c in components or []:
        content = getattr(c, "content", None)
        if isinstance(content, str):
            texts.append(content)
        children = getattr(c, "children", None)
        if children:
            texts.extend(_collect_component_text(children))
        accessory = getattr(c, "accessory", None)
        if accessory is not None:
            texts.extend(_collect_component_text([accessory]))
    return texts


def _extract_prize_and_winners(text: str) -> tuple[int, list[int]] | None:
    """Returns (amount_per_winner, [winner_id, ...]) if this text (built
    from an embed's description/title/fields and/or a Components V2
    layout's TextDisplay content — Giveaway Boat's exact layout and
    message type can vary) is a plain-JC giveaway result, else None."""
    prize_match = _JC_PRIZE_RE.search(text)
    if not prize_match:
        return None
    amount = int(prize_match.group(1).replace(",", ""))

    winner_ids = [int(uid) for uid in _MENTION_RE.findall(text)]
    if not winner_ids:
        return None

    if GIVEAWAY_SPLIT_AMONG_WINNERS and len(winner_ids) > 1:
        amount = amount // len(winner_ids)

    return amount, winner_ids


async def check_message(bot, message: discord.Message) -> bool:
    """Called from on_message for every message. Returns True if this was
    a Giveaway Boat result it handled (caller can use that to skip other
    processing, same convention as automod.check_message)."""
    if message.guild is None:
        return False

    # Diagnostic net: giveaway result messages can arrive under a
    # DIFFERENT author id than the bot's main account (e.g. sent via a
    # webhook or an interaction follow-up) even though they look
    # identical in Discord. This logs the real author id/name for
    # anything that looks like a giveaway result, regardless of the
    # configured bot id, so a mismatch is visible instead of silently
    # doing nothing.
    haystack = (message.content or "") + "\n" + "\n".join(
        (e.description or "") + "\n" + (e.title or "") for e in message.embeds
    ) + "\n" + "\n".join(_collect_component_text(message.components))
    if message.author.bot and re.search(r"won the giveaway|giveaway ended", haystack, re.IGNORECASE):
        print(
            f"[giveaways] giveaway-shaped message seen — author.id={message.author.id} "
            f"author.name={message.author!r} webhook_id={message.webhook_id} "
            f"configured_bot_id={cfg_int('giveaway_bot_id')} (match={message.author.id == cfg_int('giveaway_bot_id')})"
        )

    bot_id = cfg_int("giveaway_bot_id")
    if not bot_id or message.author.id != bot_id:
        return False

    channel_id = cfg_int("giveaway_channel_id")
    if channel_id and message.channel.id != channel_id:
        print(f"[giveaways] message from giveaway bot ignored — #{message.channel} isn't the configured giveaway channel.")
        return False

    component_texts = _collect_component_text(message.components)
    if not message.embeds and not component_texts:
        print(
            f"[giveaways] message from giveaway bot in #{message.channel} has no embeds "
            f"and no components — skipping. content={message.content!r}"
        )
        return False

    parts = [message.content or ""]
    for embed in message.embeds:
        parts.append(embed.description or "")
        parts.append(embed.title or "")
        for field in embed.fields:
            parts.append(field.name or "")
            parts.append(field.value or "")
    parts.extend(component_texts)
    text = "\n".join(parts)

    print(
        f"[giveaways] message from configured giveaway bot in #{message.channel}: "
        f"embeds={[(e.title, e.description, [(f.name, f.value) for f in e.fields]) for e in message.embeds]!r} "
        f"components_text={component_texts!r}"
    )

    parsed = _extract_prize_and_winners(text)
    if parsed is None:
        print("[giveaways] no JC-prize amount and/or winner mention found in that message — skipping.")
        return False  # not a JC prize (or not a result message) — leave it alone

    if await db.is_giveaway_processed(message.id):
        return True  # already paid out, e.g. a duplicate gateway delivery

    amount, winner_ids = parsed
    await db.mark_giveaway_processed(message.id)

    paid, failed = [], []
    for user_id in winner_ids:
        ok = await webhook.send_jc_reward(user_id, amount, reason="giveaway_win")
        member = message.guild.get_member(user_id)
        name = member.mention if member else f"<@{user_id}>"
        (paid if ok else failed).append(name)

    if paid:
        lines = [f"{emoji.CELEBRATE} **Reward distributed!**"]
        lines.append(
            f"{', '.join(paid)} — {amount:,} {emoji.JC} {emoji.JC_NAME}{'s' if amount != 1 else ''} each, sent straight to your balance."
        )
        if failed:
            lines.append(f"{emoji.WARNING} Couldn't reach the credit system for: {', '.join(failed)} — an admin may need to retry manually.")
        await message.channel.send("\n".join(lines))
    elif failed:
        await message.channel.send(
            f"{emoji.ERROR} Giveaway ended but the reward couldn't be sent for: {', '.join(failed)}. "
            f"An admin may need to retry manually."
        )

    return True


def setup(bot, owner_id: int):
    def is_owner():
        async def predicate(ctx):
            return ctx.author.id == owner_id
        return commands.check(predicate)

    @bot.command(name="setgiveawaybot")
    @is_owner()
    async def setgiveawaybot(ctx, user_id: int):
        """-setgiveawaybot <user id> — owner-only. Sets which bot's
        giveaway-result messages trigger auto-rewards."""
        config["giveaway_bot_id"] = str(user_id)
        await db.set_config("giveaway_bot_id", user_id)
        await ctx.send(f"{emoji.SUCCESS} Giveaway bot set to `{user_id}`.")

    @bot.command(name="setgiveawaychannel")
    @is_owner()
    async def setgiveawaychannel(ctx, channel: discord.TextChannel = None):
        """-setgiveawaychannel [#channel] — owner-only. Restricts
        auto-rewards to results posted in this channel. Run with no
        channel to clear the restriction (watch every channel)."""
        if channel is None:
            config["giveaway_channel_id"] = "0"
            await db.set_config("giveaway_channel_id", 0)
            await ctx.send(f"{emoji.SUCCESS} Giveaway channel restriction cleared — watching every channel.")
            return
        config["giveaway_channel_id"] = str(channel.id)
        await db.set_config("giveaway_channel_id", channel.id)
        await ctx.send(f"{emoji.SUCCESS} Only watching {channel.mention} for giveaway results now.")

    @bot.command(name="giveawayconfig")
    @is_owner()
    async def giveawayconfig(ctx):
        channel = bot.get_channel(cfg_int("giveaway_channel_id"))
        embed = discord.Embed(title=f"{emoji.CONFIG} Giveaway Reward Config", color=discord.Color.blurple())
        embed.add_field(name="Giveaway Bot ID", value=f"`{cfg_int('giveaway_bot_id') or 'Not set'}`", inline=False)
        embed.add_field(name="Watched Channel", value=channel.mention if channel else "Any channel", inline=False)
        embed.add_field(
            name="Split Prize Among Winners",
            value="Yes" if GIVEAWAY_SPLIT_AMONG_WINNERS else "No — each winner gets the full amount",
            inline=False,
        )
        await ctx.send(embed=embed)
