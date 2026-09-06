import discord
from discord.ext import commands, tasks
import os
import time
import asyncio

import db
import vanity
import webhook

intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True

bot = commands.Bot(command_prefix="-", intents=intents)

# Only this Discord user ID can run the config commands below.
# Replace 0 with your own Discord user ID (Developer Mode -> right-click
# your name -> Copy User ID).
OWNER_ID = 0


def is_owner():
    async def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("🚫 Only the bot owner can use this command.")
    else:
        raise error


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Vanity bot is running.")


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    guild_id = vanity.cfg_int("guild_id")
    if guild_id and after.guild.id != guild_id:
        return

    had = vanity.has_vanity(before)
    has = vanity.has_vanity(after)

    if has and not had:
        await vanity.apply_vanity_added(bot, after)
    elif had and not has:
        await vanity.apply_vanity_removed(bot, after)


_scan_done = False


@bot.listen("on_ready")
async def start_scan_once():
    global _scan_done
    if not _scan_done:
        _scan_done = True
        await vanity.initial_scan(bot)


# ---------------------------------------------------------------------------
# Presence: keep the bot's status showing live member count
# ---------------------------------------------------------------------------

async def refresh_presence():
    guild_id = vanity.cfg_int("guild_id")
    guild = bot.get_guild(guild_id) if guild_id else None

    if guild is None:
        # No guild locked via !setguild yet — fall back to the first
        # server the bot is in, since this bot is meant for one server.
        guild = bot.guilds[0] if bot.guilds else None

    member_count = guild.member_count if guild else 0
    activity = discord.CustomActivity(name=f"J.A.R.V.I.S. : {member_count:,} members")
    await bot.change_presence(activity=activity)


@tasks.loop(minutes=10)
async def presence_loop():
    await refresh_presence()


@bot.listen("on_ready")
async def start_presence_loop():
    if not presence_loop.is_running():
        presence_loop.start()  # fires once immediately, then every 10 min


@bot.event
async def on_member_join(member: discord.Member):
    await refresh_presence()


@bot.event
async def on_member_remove(member: discord.Member):
    await refresh_presence()


# ---------------------------------------------------------------------------
# 24h vanity → JC reward cycle (checked periodically so it fires even if
# someone never removes their vanity link)
# ---------------------------------------------------------------------------

@tasks.loop(minutes=1)
async def cycle_reward_loop():
    await vanity.check_cycle_progress(bot)


@bot.listen("on_ready")
async def start_cycle_reward_loop():
    if not cycle_reward_loop.is_running():
        cycle_reward_loop.start()


# ---------------------------------------------------------------------------
# Admin commands (configure the bot without editing files)
# ---------------------------------------------------------------------------

@bot.command(name="setvanity")
@is_owner()
async def setvanity(ctx, *, text: str):
    vanity.config["vanity_text"] = text
    await db.set_config("vanity_text", text)
    await ctx.send(f"✅ Vanity text set to `{text}`")


@bot.command(name="setrole")
@is_owner()
async def setrole(ctx, role: discord.Role):
    vanity.config["role_id"] = str(role.id)
    await db.set_config("role_id", role.id)
    await ctx.send(f"✅ Vanity role set to {role.mention}")


@bot.command(name="setlogchannel")
@is_owner()
async def setlogchannel(ctx, channel: discord.TextChannel):
    vanity.config["log_channel_id"] = str(channel.id)
    await db.set_config("log_channel_id", channel.id)
    await ctx.send(f"✅ Log channel set to {channel.mention}")


@bot.command(name="setguild")
@is_owner()
async def setguild(ctx):
    vanity.config["guild_id"] = str(ctx.guild.id)
    await db.set_config("guild_id", ctx.guild.id)
    await ctx.send("✅ This server is now the tracked guild.")


@bot.command(name="vanityconfig")
@is_owner()
async def vanityconfig(ctx):
    role = ctx.guild.get_role(vanity.cfg_int("role_id"))
    channel = bot.get_channel(vanity.cfg_int("log_channel_id"))
    embed = discord.Embed(title="⚙️ Vanity Bot Config", color=discord.Color.blurple())
    embed.add_field(name="Vanity Text", value=f"`{vanity.config.get('vanity_text')}`", inline=False)
    embed.add_field(name="Role", value=role.mention if role else "Not set", inline=True)
    embed.add_field(name="Log Channel", value=channel.mention if channel else "Not set", inline=True)
    embed.add_field(name="Guild Lock", value=str(vanity.cfg_int("guild_id") or "Any server"), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="testreward")
@is_owner()
async def testreward(ctx, member: discord.Member = None, amount: int = None):
    """!testreward [@user] [amount] — owner-only. Manually fires the full
    reward flow (webhook call to Jarvis + log embed + DM) without waiting
    for a real 24h cycle. Defaults to yourself and the normal reward
    amount if not specified. Does NOT touch the user's actual cycle
    progress in the DB — it's purely for testing the Jarvis connection
    and DM delivery."""
    member = member or ctx.author
    test_amount = amount if amount is not None else vanity.REWARD_AMOUNT_JC
    await ctx.send(f"🧪 Testing reward flow for {member.mention} — {test_amount:,} JC...")
    await vanity.grant_cycle_reward(bot, member, amount=test_amount)
    await ctx.send("✅ Test complete — check the log channel and the target user's DMs.")


@bot.command(name="vanitytime")
async def vanitytime(ctx, member: discord.Member = None):
    member = member or ctx.author
    udata = await db.get_user(member.id)
    total = udata["total_seconds"]
    cycle = udata["cycle_seconds"]
    if udata["active"]:
        elapsed = time.time() - udata["session_start"]
        total += elapsed
        cycle += elapsed
    remaining = max(0, vanity.REWARD_THRESHOLD_SECONDS - cycle)
    embed = discord.Embed(
        title="🎉 Vanity Time",
        description=f"{member.mention}'s vanity stats:",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Total (lifetime)", value=vanity.format_duration(total), inline=True)
    embed.add_field(name="Current 24h cycle", value=vanity.format_duration(cycle), inline=True)
    embed.add_field(
        name="Next reward in",
        value=vanity.format_duration(remaining) if remaining > 0 else "Any moment now 🎁",
        inline=True,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

async def main():
    await db.init_db()
    await vanity.load_config_from_db()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "No bot token found. Set the DISCORD_TOKEN environment variable "
            "on bot-hosting.net."
        )

    try:
        await bot.start(token)
    finally:
        await webhook.close_session()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
